"""
Multi-Agent Coordinator — v2 (RL-Enhanced)
-------------------------------------------
Drop-in replacement for coordinator.py.

Key upgrades vs v1:

1. REAL PARALLEL EXECUTION
   When PlannerAgent sets parallel_execution=True, RAGAgent + SearchAgent
   run concurrently via asyncio.gather().  v1 always ran them sequentially
   even though the planner requested parallel mode.

2. EVALUATORAGENT INTEGRATED
   After AnswerAgent, EvaluatorAgent scores the answer on 4 dimensions.
   The composite_score becomes the terminal RL reward instead of the old
   heuristic (answer_result.confidence + "**Sources" in output).

3. RICHER next_state_data for DQN
   6-dimensional state passed to RLDecisionAgent.record_experience():
       confidence, retrieved_count, complexity,
       has_internet, source_diversity, step_ratio   ← NEW
   These map directly to the DQN's 6D continuous state vector.

4. DYNAMIC top_k from RL agent
   When RL agent selects RETRIEVE_MORE, it now stores rl_next_top_k in
   state.metadata (set by RLDecisionAgent v2 based on query complexity).
   _run_rag() reads this instead of always adding +3.

Pipeline (unchanged externally, upgraded internally):
    User Query
        │
        ▼
    PlannerAgent          ← classifies query, determines complexity
        │
        ▼
    Initial RAG retrieval  ← always runs first
        │
        ▼
    RL Decision Loop (max 5 steps)
        ├── RETRIEVE_MORE  → RAGAgent (dynamic top_k)
        ├── RE_RANK        → RLDecisionAgent.re_rank_chunks()
        ├── ASK_CLARIFICATION → falls back to ANSWER_NOW
        └── ANSWER_NOW     → exits loop
        │
        ▼
    [parallel if plan requests]
    RAGAgent  ╔═════════════════╗
              ║  asyncio.gather ║   ← NEW
    SearchAgent╚═════════════════╝
        │
        ▼
    AnswerAgent            ← generates final answer
        │
        ▼
    EvaluatorAgent         ← scores answer (NEW)
        │
        ▼
    record_experience()    ← reward from EvaluationResult.composite_score
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from apps.rag.services.agents.base_agent    import AgentState
from apps.rag.services.agents.planner_agent import PlannerAgent
from apps.rag.services.agents.rag_agent     import RAGAgent
from apps.rag.services.agents.answer_agent  import AnswerAgent
from apps.rag.services.agents.search_agent  import SearchAgent
from apps.rag.services.agents.rl_agent      import RLDecisionAgent
from apps.rag.services.agents.evaluator_agent import EvaluatorAgent   # ← NEW

logger = logging.getLogger(__name__)


class MultiAgentCoordinator:
    """
    Central coordinator for the RL-enhanced multi-agent RAG system — v2.

    Injected services (all singletons created in views.py):
        llm_service       – AsyncGroq wrapper
        vector_store      – ChromaDB wrapper
        embedding_service – SentenceTransformer wrapper
        tavily_client     – optional; enables web search
    """

    MAX_RL_STEPS = 5

    def __init__(
        self,
        llm_service,
        vector_store,
        embedding_service,
        tavily_client=None,
    ) -> None:
        self.llm_service       = llm_service
        self.vector_store      = vector_store
        self.embedding_service = embedding_service

        # Agents
        self.planner      = PlannerAgent(llm_service=llm_service)
        self.rl_agent     = RLDecisionAgent(llm_service=llm_service)
        self.rag_agent    = RAGAgent(
            llm_service       = llm_service,
            vector_store      = vector_store,
            embedding_service = embedding_service,
        )
        self.search_agent = SearchAgent(
            llm_service   = llm_service,
            tavily_client = tavily_client,
        )
        self.answer_agent = AnswerAgent(llm_service=llm_service)
        self.evaluator    = EvaluatorAgent(llm_service=llm_service)   # ← NEW

        logger.info("[Coordinator v2] RL-Enhanced Multi-Agent Coordinator initialised")

    # ─────────────────────────────────────────────────────────────────────────
    #  PUBLIC ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(
        self,
        query:   str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Full pipeline execution.

        Args:
            query:   User's question
            context: Dict from views.py — may contain document_id,
                     document_filter, top_k, session_id, strategy

        Returns:
            Result dict with: answer, strategy_used, retrieved_chunks,
            confidence, execution_steps, internet_sources, rl_metadata,
            evaluation_result  ← NEW
        """
        start        = time.time()
        query_id_str = str(uuid.uuid4())

        state = AgentState(
            agent_name = "coordinator",
            query      = query,
            context    = context.copy(),
        )
        state.metadata["query_id"] = query_id_str

        try:
            # ── 1. PLANNER ────────────────────────────────────────────────
            plan_result = await self.planner.execute(state)
            self._extract_plan_metadata(plan_result, state)

            # ── 2. INITIAL RAG RETRIEVAL ──────────────────────────────────
            await self._run_rag(state)

            # ── 3. RL DECISION LOOP ───────────────────────────────────────
            for step in range(self.MAX_RL_STEPS):
                rl_result   = await self.rl_agent.execute(state)
                action_name = rl_result.output.strip()

                logger.info(
                    f"[Coordinator v2] RL step {step + 1}/{self.MAX_RL_STEPS} "
                    f"→ {action_name}"
                )

                if action_name == "ANSWER_NOW" or step == self.MAX_RL_STEPS - 1:
                    break

                elif action_name == "RETRIEVE_MORE":
                    await self._run_rag(state, extra=True)
                    self._record_non_terminal(state, query_id_str)

                elif action_name == "RE_RANK":
                    await self._do_rerank(query, state)
                    self._record_non_terminal(state, query_id_str)

                elif action_name == "ASK_CLARIFICATION":
                    logger.info("[Coordinator v2] ASK_CLARIFICATION → ANSWER_NOW")
                    break

            # ── 4. PARALLEL RAG + SEARCH  ← KEY UPGRADE ──────────────────
            plan_needs_internet  = state.metadata.get("requires_internet", False)
            plan_uses_parallel   = state.metadata.get("parallel_execution", False)
            has_internet_already = "search_results" in state.metadata

            if plan_uses_parallel and plan_needs_internet and not has_internet_already:
                # Run RAG and Search concurrently
                logger.info("[Coordinator v2] Running RAG + Search in PARALLEL")
                rag_task    = asyncio.create_task(self._run_rag(state, extra=True))
                search_task = asyncio.create_task(self._run_search(state))
                await asyncio.gather(rag_task, search_task, return_exceptions=True)

            elif plan_needs_internet and not has_internet_already:
                # Sequential search only (parallel not requested)
                await self._run_search(state)

            # ── 5. ANSWER GENERATION ──────────────────────────────────────
            answer_result = await self.answer_agent.execute(state)

            # Store final answer in metadata so EvaluatorAgent can read it
            state.metadata["final_answer"] = answer_result.output or ""

            # ── 6. EVALUATION (EvaluatorAgent)  ← KEY UPGRADE ────────────
            eval_result = await self.evaluator.evaluate(
                query   = query,
                answer  = answer_result.output or "",
                chunks  = state.metadata.get("retrieved_chunks", []),
                state   = state,
            )
            final_confidence = eval_result.composite_score

            # ── 7. TERMINAL REWARD using composite_score ──────────────────
            has_citations = "**Sources" in (answer_result.output or "")
            chunks        = state.metadata.get("retrieved_chunks", [])
            n_chunks      = len(chunks)
            unique_sources = len({
                c.get("metadata", {}).get("source", f"__{i}")
                for i, c in enumerate(chunks)
            }) if chunks else 0

            next_state_data = {
                "confidence":       final_confidence,
                "retrieved_count":  n_chunks,
                "complexity":       state.metadata.get("query_complexity", "medium"),
                "has_internet":     "search_results" in state.metadata,
                "has_citations":    has_citations,
                "evaluation_result": eval_result.to_dict(),        # ← NEW
                "source_diversity": unique_sources / max(n_chunks, 1),
                "step_ratio":       state.metadata.get("rl_step_count", 0) / self.MAX_RL_STEPS,
            }

            self.rl_agent.record_experience(
                state           = state,
                next_state_data = next_state_data,
                done            = True,
                query_id        = query_id_str,
            )

            # ── 8. BUILD RESPONSE ─────────────────────────────────────────
            return self._build_response(
                query        = query,
                answer       = answer_result.output or "",
                state        = state,
                confidence   = final_confidence,
                eval_result  = eval_result,
                start        = start,
                query_id_str = query_id_str,
            )

        except Exception as exc:
            logger.error(f"[Coordinator v2] execute failed: {exc}", exc_info=True)
            raise

    # ─────────────────────────────────────────────────────────────────────────
    #  AGENT RUNNERS
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_rag(self, state: AgentState, extra: bool = False) -> None:
        """
        Run RAGAgent and merge results into state.

        v2 change: RETRIEVE_MORE now reads rl_next_top_k from state.metadata
        (set by RLDecisionAgent based on complexity) instead of always +3.
        """
        if extra:
            # Use dynamic k from RL agent if available, otherwise +3 fallback
            dynamic_k = state.metadata.pop("rl_next_top_k", None)
            if dynamic_k is not None:
                state.context["top_k"] = dynamic_k
                logger.info(
                    f"[Coordinator v2] RETRIEVE_MORE → dynamic top_k={dynamic_k}"
                )
            else:
                state.context["top_k"] = state.context.get("top_k", 5) + 3
                logger.info(
                    f"[Coordinator v2] RETRIEVE_MORE → fallback top_k={state.context['top_k']}"
                )

        result    = await self.rag_agent.execute(state)
        new_chunks = result.metadata.get("retrieved_chunks", [])

        if not new_chunks:
            new_chunks = state.metadata.get("retrieved_chunks", [])

        if new_chunks:
            existing = state.metadata.get("retrieved_chunks", [])
            seen     = {c.get("content", "") for c in existing}
            merged   = existing + [c for c in new_chunks if c.get("content", "") not in seen]
            state.metadata["retrieved_chunks"] = merged
            logger.info(
                f"[Coordinator v2] RAG stored {len(merged)} chunks total "
                f"(success={result.success})"
            )
        else:
            logger.warning("[Coordinator v2] RAG returned 0 chunks")

        if "relevance_check" in result.metadata:
            state.metadata["relevance_check"] = result.metadata["relevance_check"]

    async def _do_rerank(self, query: str, state: AgentState) -> None:
        """Re-rank existing chunks using the RL agent's LLM scorer."""
        chunks = state.metadata.get("retrieved_chunks", [])
        if not chunks:
            return
        re_ranked = await self.rl_agent.re_rank_chunks(query, chunks, state)
        state.metadata["retrieved_chunks"] = re_ranked
        if re_ranked:
            top_score = re_ranked[0].get("rl_rerank_score", 5) / 10.0
            state.metadata.setdefault("relevance_check", {})["score"] = top_score

    async def _run_search(self, state: AgentState) -> None:
        """Run SearchAgent and store results in state."""
        result = await self.search_agent.execute(state)
        if result.success:
            state.metadata.update(result.metadata)

    # ─────────────────────────────────────────────────────────────────────────
    #  REWARD HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _record_non_terminal(
        self,
        state:        AgentState,
        query_id_str: str,
    ) -> None:
        """Record reward for a non-terminal RETRIEVE_MORE / RE_RANK action."""
        chunks    = state.metadata.get("retrieved_chunks", [])
        n_chunks  = len(chunks)
        unique_s  = len({
            c.get("metadata", {}).get("source", f"__{i}")
            for i, c in enumerate(chunks)
        }) if chunks else 0

        next_data = {
            "confidence":      state.metadata.get("relevance_check", {}).get("score", 0.5),
            "retrieved_count": n_chunks,
            "complexity":      state.metadata.get("query_complexity", "medium"),
            "has_internet":    "search_results" in state.metadata,
            "has_citations":   False,
            "source_diversity": unique_s / max(n_chunks, 1),
            "step_ratio":      state.metadata.get("rl_step_count", 0) / self.MAX_RL_STEPS,
        }
        self.rl_agent.record_experience(
            state           = state,
            next_state_data = next_data,
            done            = False,
            query_id        = query_id_str,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  PLAN EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_plan_metadata(self, plan_result, state: AgentState) -> None:
        """Pull planner output into state.metadata."""
        import json
        try:
            plan_data = json.loads(plan_result.output or "{}")
            state.metadata["query_complexity"]   = plan_data.get("complexity", "medium")
            state.metadata["query_type"]         = plan_data.get("query_type", "factual_question")
            exec_plan = plan_data.get("execution_plan", {})
            state.metadata["requires_internet"]  = exec_plan.get("requires_internet", False)
            state.metadata["parallel_execution"] = exec_plan.get("parallel_execution", False)
            logger.info(
                f"[Coordinator v2] Plan: "
                f"type={state.metadata['query_type']} | "
                f"complexity={state.metadata['query_complexity']} | "
                f"internet={state.metadata['requires_internet']} | "
                f"parallel={state.metadata['parallel_execution']}"
            )
        except Exception:
            state.metadata.setdefault("query_complexity",   "medium")
            state.metadata.setdefault("query_type",         "factual_question")
            state.metadata.setdefault("requires_internet",  False)
            state.metadata.setdefault("parallel_execution", False)

    # ─────────────────────────────────────────────────────────────────────────
    #  RESPONSE BUILDER
    # ─────────────────────────────────────────────────────────────────────────

    def _build_response(
        self,
        query:        str,
        answer:       str,
        state:        AgentState,
        confidence:   float,
        eval_result,
        start:        float,
        query_id_str: str,
    ) -> Dict[str, Any]:
        """Build the final dict returned to views.py."""
        chunks          = state.metadata.get("retrieved_chunks", [])
        internet_data   = state.metadata.get("search_results", {})
        internet_sources = (
            internet_data.get("sources", []) if isinstance(internet_data, dict) else []
        )

        execution_steps: List[Dict] = [
            {
                "step_number": s.step_number,
                "type":        s.step_type,
                "content":     s.content,
                "timestamp":   s.timestamp,
                "metadata":    s.metadata,
            }
            for s in state.execution_steps
        ]

        rl_metadata = {
            "query_id":         query_id_str,
            "steps_taken":      state.metadata.get("rl_step_count",    0),
            "last_action":      state.metadata.get("rl_action_name",   "ANSWER_NOW"),
            "last_reward":      state.metadata.get("rl_reward",        0.0),
            "epsilon":          round(self.rl_agent.memory.epsilon, 4),
            "total_dqn_updates":self.rl_agent.memory.total_updates,
            "recent_dqn_loss":  round(self.rl_agent.memory.recent_loss, 6),
        }

        return {
            "answer":            answer,
            "confidence":        confidence,
            "retrieved_chunks":  [
                {
                    "content":  c.get("content", ""),
                    "score":    c.get("score", 0.0),
                    "metadata": c.get("metadata", {}),
                }
                for c in chunks
            ],
            "execution_steps":   execution_steps,
            "internet_sources":  internet_sources,
            "source":            "rl_multi_agent_v2",
            "agent_type":        "rl_coordinator_v2",
            "agents_used":       [
                "PlannerAgent", "RLDecisionAgent", "RAGAgent",
                "AnswerAgent",  "EvaluatorAgent",             # ← NEW
            ],
            "query_type":        state.metadata.get("query_type", "factual_question"),
            "rl_metadata":       rl_metadata,
            "evaluation_result": eval_result.to_dict() if eval_result else {},  # ← NEW
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  AGENT STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_agent_status(self) -> Dict[str, Any]:
        """Called by views.agent_status — includes DQN stats and evaluator."""
        agents = [
            self.planner, self.rl_agent,
            self.rag_agent, self.search_agent,
            self.answer_agent, self.evaluator,        # ← evaluator added
        ]
        return {
            "agents":    [a.get_capabilities() for a in agents],
            "rl_stats":  self.rl_agent.get_rl_stats(),
            "rl_enabled": True,
            "dqn_enabled": True,
            "parallel_execution_enabled": True,
        }