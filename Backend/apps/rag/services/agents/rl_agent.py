"""
RL Decision Agent — v2
-----------------------
Drop-in replacement for the v1 RLDecisionAgent.

Key upgrades vs v1:
    1. Uses RLMemoryManager v2 (DQN) instead of tabular Q-table
    2. State is 6-dimensional continuous vector (v1 had 4 discrete buckets)
    3. Terminal reward uses EvaluatorAgent.composite_score instead of
       the old heuristic (confidence score + citations flag)
    4. RETRIEVE_MORE is now DYNAMIC — top_k chosen from {3, 7, 15}
       based on query complexity, not always +3
    5. Action logging shows DQN Q-values and recent loss

6D State vector (see rl_memory.py for full spec):
    [0] confidence        [1] retrieval_density  [2] complexity
    [3] has_internet      [4] source_diversity   [5] step_ratio

Reward shaping (unchanged API, better signal):
    +1.0  composite_score ≥ 0.75  (EvaluatorAgent HIGH_QUALITY)
    +0.30  composite_score ≥ 0.50  (ACCEPTABLE)
    -1.0  composite_score < 0.50  (LOW_QUALITY)
    +0.50  answer has citations
    +0.30  RETRIEVE_MORE raised confidence by > 0.10
    -0.50  RETRIEVE_MORE did NOT raise confidence
    +0.20  RE_RANK and confidence > 0.60 after
    -0.10  RE_RANK and confidence still ≤ 0.60
    -0.05  per-step cost (encourages efficiency)
    +0.30  deferred user positive feedback
    -0.30  deferred user negative feedback
"""

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .base_agent import BaseAgent, AgentResult, AgentState
from apps.rag.services.agents.rl_memory import (
    ACTIONS, ACTION_IDX, N_ACTIONS, STATE_DIM, MAX_CHUNKS,
    RLExperience, RLMemoryManager,
)

logger = logging.getLogger(__name__)


class RLDecisionAgent(BaseAgent):
    """
    Reinforcement Learning Decision Agent — v2.

    Same external interface as v1 so coordinator.py needs minimal changes.
    Internal representation upgraded to 6D continuous state + DQN.
    """

    # Reward constants (same names as v1 for backward compat)
    R_HIGH_CONF    = +1.00
    R_CITATION     = +0.50
    R_USEFUL_RETR  = +0.30
    R_USELESS_RETR = -0.50
    R_LOW_CONF     = -1.00
    R_STEP_COST    = -0.05
    R_USER_POS     = +0.30
    R_USER_NEG     = -0.30

    MAX_STEPS = 5

    # Dynamic top_k choices per complexity level
    RETRIEVE_K = {
        "simple":  3,
        "medium":  7,
        "complex": 15,
    }

    def __init__(self, llm_service=None) -> None:
        super().__init__(
            name        = "RLDecisionAgent",
            description = "DQN-based decision: retrieve / re-rank / answer",
            llm_service = llm_service,
        )
        self.memory = RLMemoryManager()

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN EXECUTE
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(self, state: AgentState) -> AgentResult:
        """
        Select the next action using the DQN.

        Returns AgentResult whose `output` field is the action name string
        e.g. "RETRIEVE_MORE" — coordinator reads this to decide what to do.
        """
        try:
            rl_state    = self._build_state(state)           # np.ndarray shape (6,)
            action_idx  = self.memory.select_action(rl_state, training=True)
            action_name = ACTIONS[action_idx]

            # Hard cap at MAX_STEPS
            step_count = state.metadata.get("rl_step_count", 0)
            if step_count >= self.MAX_STEPS - 1:
                action_idx  = ACTION_IDX["ANSWER_NOW"]
                action_name = "ANSWER_NOW"
                self.add_observation(
                    state, f"Max steps ({self.MAX_STEPS}) reached → ANSWER_NOW"
                )

            # If RETRIEVE_MORE — compute dynamic top_k and store it
            if action_name == "RETRIEVE_MORE":
                complexity  = state.metadata.get("query_complexity", "medium")
                dynamic_k   = self.RETRIEVE_K.get(str(complexity), 7)
                state.metadata["rl_next_top_k"] = dynamic_k
                self.add_observation(
                    state,
                    f"RETRIEVE_MORE: dynamic top_k={dynamic_k} for complexity={complexity}"
                )

            # Persist to state
            state.metadata["rl_state"]       = rl_state
            state.metadata["rl_action"]      = action_idx
            state.metadata["rl_action_name"] = action_name
            state.metadata["rl_step_count"]  = step_count + 1

            # Log Q-values from DQN
            q_vals  = self.memory.q_values_for_state(rl_state)
            q_table = {ACTIONS[i]: round(float(v), 3) for i, v in enumerate(q_vals)}

            self.add_thought(state, f"DQN state: {rl_state.round(3).tolist()}")
            self.add_action(state, f"Selected action: {action_name}", "dqn_qtable")
            self.add_observation(
                state,
                f"DQN Q-values → {q_table} | ε={self.memory.epsilon:.4f} | "
                f"loss={self.memory.recent_loss:.4f}"
            )

            return self.create_result(
                success    = True,
                output     = action_name,
                state      = state,
                confidence = 0.9,
            )

        except Exception as exc:
            logger.error(f"[RLDecisionAgent v2] execute error: {exc}", exc_info=True)
            state.metadata["rl_action_name"] = "ANSWER_NOW"
            return self.create_result(
                success=True, output="ANSWER_NOW", state=state, confidence=0.5,
            )

    # ─────────────────────────────────────────────────────────────────────────
    #  REWARD + EXPERIENCE STORAGE
    # ─────────────────────────────────────────────────────────────────────────

    def record_experience(
        self,
        state:           AgentState,
        next_state_data: Dict[str, Any],
        done:            bool,
        query_id:        str = "",
    ) -> float:
        """
        Compute reward, push to replay buffer, update DQN.
        Called by coordinator after every action completes.

        Args:
            state:            Agent state BEFORE the action
            next_state_data:  Dict with keys: confidence, retrieved_count,
                              complexity, has_internet, has_citations,
                              evaluation_result (optional EvaluatorAgent output)
            done:             True on the final (ANSWER_NOW) step
            query_id:         DB UUID of the query (for deferred feedback)

        Returns:
            Computed reward (float)
        """
        prev_state = state.metadata.get("rl_state")
        action_idx = state.metadata.get("rl_action", ACTION_IDX["ANSWER_NOW"])

        if prev_state is None:
            return 0.0

        reward        = self._compute_reward(state, next_state_data, done)
        next_rl_state = self._state_from_dict(next_state_data)

        # Push to replay buffer
        experience = RLExperience(
            state      = prev_state,
            action     = action_idx,
            reward     = reward,
            next_state = next_rl_state,
            done       = done,
            query_id   = str(query_id),
        )
        self.memory.replay_buf.push(experience)

        # Immediate DQN update
        self.memory.update_dqn(prev_state, action_idx, reward, next_rl_state, done)

        # Periodic replay training every 10 new experiences
        if len(self.memory.replay_buf) % 10 == 0:
            self.memory.replay_train(batch_size=32)

        # Persist on terminal step
        if done:
            self.memory.save()

        state.metadata["rl_reward"] = reward

        # Persist to DB
        self._save_experience_to_db(
            query_id      = query_id,
            rl_state      = prev_state.tolist(),
            action_idx    = action_idx,
            reward        = reward,
            next_rl_state = next_rl_state.tolist(),
            done          = done,
        )

        logger.info(
            f"[RLDecisionAgent v2] reward={reward:+.3f} | "
            f"action={ACTIONS[action_idx]} | done={done} | "
            f"ε={self.memory.epsilon:.4f} | loss={self.memory.recent_loss:.4f}"
        )
        return reward

    def apply_user_feedback(self, query_id: str, feedback: str) -> None:
        """
        Deferred user feedback reward.  Called from views.rl_feedback().
        Applies ±0.30 delta to all Q-table entries for this query.
        """
        from apps.rag.models import RLExperienceRecord

        delta = self.R_USER_POS if feedback == "positive" else self.R_USER_NEG

        try:
            records = RLExperienceRecord.objects.filter(query_id=query_id)
            for rec in records:
                s = np.array(rec.rl_state, dtype=float)
                a = rec.action_idx
                # Apply to DQN (single-step update, no next state needed)
                self.memory.update_dqn(s, a, delta, s, done=True)
                rec.user_feedback = feedback
                rec.reward       += delta
                rec.save()

            self.memory.save()
            logger.info(
                f"[RLDecisionAgent v2] User feedback '{feedback}' applied "
                f"to {records.count()} records for query {query_id} | δ={delta:+.2f}"
            )
        except Exception as exc:
            logger.error(f"[RLDecisionAgent v2] apply_user_feedback error: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    #  RE-RANKING HELPER (unchanged from v1)
    # ─────────────────────────────────────────────────────────────────────────

    async def re_rank_chunks(
        self,
        query:  str,
        chunks: list,
        state:  AgentState,
    ) -> list:
        """
        LLM-based re-ranking.  Called when RL selects RE_RANK action.
        Scores each chunk 1-10 for relevance, returns sorted list.
        Falls back to original order on any failure.
        """
        if not chunks or not self.llm_service:
            return chunks

        self.add_action(state, "Re-ranking chunks via LLM", "re_ranker")

        try:
            scored = []
            for chunk in chunks[:8]:   # cap at 8 to stay within token budget
                snippet = chunk.get("content", "")[:300]
                prompt  = (
                    f"Rate relevance for answering the question.\n"
                    f"Question: {query}\n"
                    f"Text: {snippet}\n"
                    f"Reply ONLY with a number 1 (irrelevant) to 10 (perfect match)."
                )
                resp = await self.call_llm(prompt, temperature=0.0, max_tokens=5)
                try:
                    score = float(resp.strip().split()[0])
                    score = max(1.0, min(10.0, score))
                except (ValueError, IndexError):
                    score = 5.0
                scored.append({**chunk, "rl_rerank_score": score})

            ranked = sorted(scored, key=lambda c: c.get("rl_rerank_score", 0), reverse=True)
            self.add_observation(state, f"Re-ranked {len(ranked)} chunks")
            return ranked

        except Exception as exc:
            logger.warning(f"[RLDecisionAgent v2] re_rank_chunks fallback: {exc}")
            return chunks

    # ─────────────────────────────────────────────────────────────────────────
    #  6D STATE BUILDER
    # ─────────────────────────────────────────────────────────────────────────

    def _build_state(self, state: AgentState) -> np.ndarray:
        """
        Build 6-dimensional continuous state vector from live AgentState.

        Dimensions:
            [0] confidence        — raw float from relevance_check.score
            [1] retrieval_density — n_chunks / MAX_CHUNKS
            [2] complexity        — 0.0 / 0.5 / 1.0
            [3] has_internet      — 0.0 or 1.0
            [4] source_diversity  — unique sources / n_chunks (or 0)
            [5] step_ratio        — steps_taken / MAX_STEPS
        """
        meta    = state.metadata
        context = state.context

        # [0] confidence
        confidence = float(meta.get("relevance_check", {}).get("score", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        # [1] retrieval_density
        chunks         = meta.get("retrieved_chunks", [])
        n_chunks       = len(chunks)
        retr_density   = min(1.0, n_chunks / MAX_CHUNKS)

        # [2] complexity
        comp_str  = str(meta.get("query_complexity", "medium"))
        comp_val  = {"simple": 0.0, "medium": 0.5, "complex": 1.0}.get(comp_str, 0.5)

        # [3] has_internet
        has_internet = float(
            "search_results" in meta or "tavily_answer" in meta
        )

        # [4] source_diversity (unique source files / n_chunks)
        if chunks:
            unique_sources = len({
                c.get("metadata", {}).get("source", f"__{i}")
                for i, c in enumerate(chunks)
            })
            diversity = unique_sources / n_chunks
        else:
            diversity = 0.0

        # [5] step_ratio
        step_ratio = min(1.0, meta.get("rl_step_count", 0) / self.MAX_STEPS)

        return np.array(
            [confidence, retr_density, comp_val, has_internet, diversity, step_ratio],
            dtype=float,
        )

    def _state_from_dict(self, d: Dict[str, Any]) -> np.ndarray:
        """Build 6D state from a plain dict (used for next_state in record_experience)."""
        confidence   = float(max(0.0, min(1.0, d.get("confidence", 0.5))))
        retr_density = min(1.0, d.get("retrieved_count", 0) / MAX_CHUNKS)
        comp_val     = {"simple": 0.0, "medium": 0.5, "complex": 1.0}.get(
                            d.get("complexity", "medium"), 0.5
                        )
        has_internet  = float(d.get("has_internet",   False))
        diversity     = float(max(0.0, min(1.0, d.get("source_diversity", 0.0))))
        step_ratio    = float(max(0.0, min(1.0, d.get("step_ratio",       0.0))))

        return np.array(
            [confidence, retr_density, comp_val, has_internet, diversity, step_ratio],
            dtype=float,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  REWARD COMPUTATION  (upgraded to use EvaluatorAgent composite_score)
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_reward(
        self,
        state:     AgentState,
        next_data: Dict[str, Any],
        done:      bool,
    ) -> float:
        """
        Compute scalar reward.

        Terminal step:
            - Uses EvaluationResult.composite_score from EvaluatorAgent if present
            - Falls back to confidence heuristic (same as v1) if evaluator absent
        Non-terminal:
            - RETRIEVE_MORE: reward based on confidence improvement
            - RE_RANK: reward based on post-rerank confidence
        Always:
            - Step cost −0.05 (encourages efficiency)
        """
        action_name = ACTIONS[state.metadata.get("rl_action", ACTION_IDX["ANSWER_NOW"])]
        reward      = self.R_STEP_COST    # always pay step cost

        if done:
            # ── Primary: use EvaluatorAgent score ─────────────────────────
            eval_result = next_data.get("evaluation_result", {})
            if eval_result:
                composite = float(eval_result.get("composite_score", 0.5))
                if composite >= 0.75:
                    reward += self.R_HIGH_CONF
                elif composite >= 0.50:
                    reward += 0.30
                else:
                    reward += self.R_LOW_CONF
                # Bonus if hallucination_risk is low
                if float(eval_result.get("hallucination_risk", 1.0)) < 0.30:
                    reward += 0.20

            else:
                # ── Fallback: same heuristic as v1 ────────────────────────
                conf = next_data.get("confidence", 0.5)
                reward += self.R_HIGH_CONF if conf >= 0.75 else (
                           0.30           if conf >= 0.50 else
                           self.R_LOW_CONF)

            # Citation bonus (same as v1)
            if next_data.get("has_citations", False):
                reward += self.R_CITATION

        else:
            if action_name == "RETRIEVE_MORE":
                prev_conf = state.metadata.get("relevance_check", {}).get("score", 0.5)
                new_conf  = next_data.get("confidence", prev_conf)
                reward   += (
                    self.R_USEFUL_RETR
                    if new_conf > prev_conf + 0.10
                    else self.R_USELESS_RETR
                )

            elif action_name == "RE_RANK":
                reward += 0.20 if next_data.get("confidence", 0.5) > 0.60 else -0.10

        return round(reward, 4)

    # ─────────────────────────────────────────────────────────────────────────
    #  DB PERSISTENCE (unchanged from v1)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _save_experience_to_db(
        query_id:      str,
        rl_state:      list,
        action_idx:    int,
        reward:        float,
        next_rl_state: list,
        done:          bool,
    ) -> None:
        try:
            from apps.rag.models import RLExperienceRecord
            RLExperienceRecord.objects.create(
                query_id      = query_id,
                rl_state      = rl_state,
                action_idx    = action_idx,
                action_name   = ACTIONS[action_idx],
                reward        = reward,
                next_rl_state = next_rl_state,
                done          = done,
            )
        except Exception as exc:
            logger.warning(f"[RLDecisionAgent v2] DB persist skipped: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    #  STATS
    # ─────────────────────────────────────────────────────────────────────────

    def get_rl_stats(self) -> Dict[str, Any]:
        mem = self.memory
        return {
            "epsilon":         round(mem.epsilon, 4),
            "total_updates":   mem.total_updates,
            "replay_buf_size": len(mem.replay_buf),
            "recent_dqn_loss": round(mem.recent_loss, 6),
            "actions":         list(ACTIONS.values()),
            "state_dim":       STATE_DIM,
            "dqn_architecture": "6 → 32 → 32 → 4 (pure numpy)",
            "warm_start":      "800 synthetic pairs × 5 epochs",
        }

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "name":         self.name,
            "description":  self.description,
            "capabilities": [
                "Epsilon-greedy DQN policy",
                "6D continuous state space",
                "Dynamic top_k selection per complexity",
                "EvaluatorAgent reward signal integration",
                "Experience replay (capacity=10k)",
                "Target network (sync every 50 steps)",
                "Warm-start pre-training (800 synthetic pairs)",
                "Deferred user-feedback reward",
                "LLM-based chunk re-ranking",
            ],
            "actions":       list(ACTIONS.values()),
            "output_format": "action_name_string",
        }