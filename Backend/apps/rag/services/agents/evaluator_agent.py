"""
Evaluator Agent
---------------
Standalone quality evaluator.  Previously, confidence was computed inside
AnswerAgent with a simple heuristic (+0.1 per source, capped at 0.95).

Now EvaluatorAgent provides a structured 4-dimensional quality score that
becomes the PRIMARY reward signal for the RL agent — replacing the old
heuristic entirely.

Evaluation dimensions:
    factuality_score   (0-1)  — Are claims supported by retrieved context?
    coverage_score     (0-1)  — Does the answer address the full query?
    hallucination_risk (0-1)  — Probability of fabricated content (lower = better)
    conciseness_score  (0-1)  — Is length appropriate?

Composite reward signal (used by RLDecisionAgent):
    composite = 0.40 * factuality
              + 0.30 * coverage
              + 0.20 * (1 - hallucination_risk)
              + 0.10 * conciseness

Placement in pipeline (coordinator.py):
    AnswerAgent → generates answer
        ↓
    EvaluatorAgent → scores it          ← THIS FILE
        ↓
    RLDecisionAgent.record_experience() → uses composite_score as reward
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

from .base_agent import BaseAgent, AgentState, AgentResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  RESULT DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EvaluationResult:
    """
    Structured quality verdict returned by EvaluatorAgent.

    composite_score is computed automatically in __post_init__ and is the
    single float the RL agent uses as its terminal reward.
    """
    factuality_score:   float = 0.5
    coverage_score:     float = 0.5
    hallucination_risk: float = 0.5
    conciseness_score:  float = 0.5
    composite_score:    float = 0.0
    verdict:            str   = "UNKNOWN"
    issues:             List[str] = field(default_factory=list)
    raw_llm_response:   str   = ""

    # Reward weights — must sum to 1.0
    _W_FACTUALITY    = 0.40
    _W_COVERAGE      = 0.30
    _W_HALLUCINATION = 0.20   # applied as (1 - risk)
    _W_CONCISENESS   = 0.10

    def __post_init__(self) -> None:
        self.composite_score = round(
            self._W_FACTUALITY    * self.factuality_score
          + self._W_COVERAGE      * self.coverage_score
          + self._W_HALLUCINATION * (1.0 - self.hallucination_risk)
          + self._W_CONCISENESS   * self.conciseness_score,
            4,
        )
        # Verdict thresholds
        if self.composite_score >= 0.75:
            self.verdict = "HIGH_QUALITY"
        elif self.composite_score >= 0.50:
            self.verdict = "ACCEPTABLE"
        else:
            self.verdict = "LOW_QUALITY"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factuality_score":   self.factuality_score,
            "coverage_score":     self.coverage_score,
            "hallucination_risk": self.hallucination_risk,
            "conciseness_score":  self.conciseness_score,
            "composite_score":    self.composite_score,
            "verdict":            self.verdict,
            "issues":             self.issues,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  EVALUATOR AGENT
# ─────────────────────────────────────────────────────────────────────────────

class EvaluatorAgent(BaseAgent):
    """
    Scores generated answers on 4 quality dimensions using a single LLM call.

    HOW TO ADD TO EXISTING PROJECT:
        1. Drop this file into:  apps/rag/services/agents/evaluator_agent.py
        2. Import in coordinator.py:
               from apps.rag.services.agents.evaluator_agent import EvaluatorAgent, EvaluationResult
        3. Create instance in MultiAgentCoordinator.__init__():
               self.evaluator = EvaluatorAgent(llm_service=llm_service)
        4. Call after AnswerAgent:
               eval_result = await self.evaluator.evaluate(
                   query=query, answer=answer_result.output,
                   chunks=state.metadata.get("retrieved_chunks", []),
                   state=state
               )
        5. Pass eval_result.composite_score as the terminal reward
           instead of answer_result.confidence.
    """

    def __init__(self, llm_service=None) -> None:
        super().__init__(
            name        = "EvaluatorAgent",
            description = "Scores answer quality: factuality, coverage, hallucination risk",
            llm_service = llm_service,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  PRIMARY PUBLIC METHOD
    # ─────────────────────────────────────────────────────────────────────────

    async def evaluate(
        self,
        query:   str,
        answer:  str,
        chunks:  List[Dict[str, Any]],
        state:   Optional[AgentState] = None,
    ) -> EvaluationResult:
        """
        Full evaluation pipeline.

        Args:
            query:   Original user query
            answer:  Text generated by AnswerAgent
            chunks:  Retrieved context chunks (List of {"content": str, ...})
            state:   Optional AgentState for execution trace logging

        Returns:
            EvaluationResult — composite_score is the RL reward signal
        """
        if state is None:
            state = AgentState(agent_name=self.name, query=query)

        try:
            self.add_thought(state, f"Evaluating answer quality for: '{query[:60]}'")

            # ── Fast-fail guard ───────────────────────────────────────────
            if not answer or len(answer.strip()) < 10:
                result = EvaluationResult(
                    factuality_score   = 0.0,
                    coverage_score     = 0.0,
                    hallucination_risk = 1.0,
                    conciseness_score  = 0.5,
                    issues             = ["Answer is empty or too short"],
                )
                self._store_in_state(state, result)
                return result

            context_text = self._build_context_text(chunks)

            # ── LLM evaluation (primary path) ─────────────────────────────
            result = await self._llm_evaluate(query, answer, context_text, state)

            self.add_observation(
                state,
                f"Evaluation: verdict={result.verdict} "
                f"composite={result.composite_score:.3f} "
                f"factuality={result.factuality_score:.2f} "
                f"hall_risk={result.hallucination_risk:.2f}"
            )

            self._store_in_state(state, result)
            return result

        except Exception as exc:
            logger.error(f"[EvaluatorAgent] evaluate() failed: {exc}", exc_info=True)
            # Conservative fallback — medium scores, never crash pipeline
            return EvaluationResult(
                factuality_score   = 0.5,
                coverage_score     = 0.5,
                hallucination_risk = 0.4,
                conciseness_score  = 0.5,
                issues             = [f"Evaluation error: {exc}"],
            )

    # ─────────────────────────────────────────────────────────────────────────
    #  BaseAgent.execute() compatibility
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(self, state: AgentState) -> AgentResult:
        """
        Called if coordinator puts EvaluatorAgent in the agent chain directly.
        Reads query / answer / chunks from state.metadata.
        """
        query  = state.query
        answer = state.metadata.get("final_answer", "")
        chunks = state.metadata.get("retrieved_chunks", [])

        result = await self.evaluate(query, answer, chunks, state)

        return self.create_result(
            success    = True,
            output     = json.dumps(result.to_dict()),
            state      = state,
            confidence = result.composite_score,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  LLM EVALUATION (single call, all 4 dimensions)
    # ─────────────────────────────────────────────────────────────────────────

    async def _llm_evaluate(
        self,
        query:        str,
        answer:       str,
        context_text: str,
        state:        AgentState,
    ) -> EvaluationResult:
        """
        One LLM call that scores all 4 dimensions at once.
        Falls back to heuristics on JSON parse failure.
        """
        self.add_action(state, "Running LLM quality evaluation", "llm_evaluator")

        prompt = f"""You are a strict answer quality evaluator. Score the ANSWER on 4 dimensions.

QUERY:
{query}

CONTEXT (retrieved documents):
{context_text[:2000]}

ANSWER TO EVALUATE:
{answer[:1500]}

Score each dimension strictly from 0.0 to 1.0:

1. factuality_score
   - 1.0 = every claim is directly supported by the context above
   - 0.5 = most claims supported, some uncertain
   - 0.0 = claims contradict context, or context is completely ignored

2. coverage_score
   - 1.0 = the answer addresses every part of the query
   - 0.5 = partially addresses the query
   - 0.0 = query is mostly unanswered

3. hallucination_risk (LOWER is better)
   - 0.0 = answer closely follows context, no invented facts
   - 0.5 = moderate risk of added details not in context
   - 1.0 = answer clearly adds facts not present in context

4. conciseness_score
   - 1.0 = answer length is exactly right for the question
   - 0.5 = slightly too long or too short
   - 0.0 = extremely verbose or one-word response for a complex query

Also list up to 3 specific issues found. Empty list if none.

Return ONLY valid JSON — no markdown, no explanation, no other text:
{{
    "factuality_score": 0.0,
    "coverage_score": 0.0,
    "hallucination_risk": 0.0,
    "conciseness_score": 0.0,
    "issues": []
}}"""

        try:
            raw = await self.call_llm(prompt, temperature=0.1, max_tokens=300)
            raw = raw.strip()

            # Strip markdown fences if LLM wraps response
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            data = json.loads(raw)

            def clamp(v: Any) -> float:
                return float(max(0.0, min(1.0, v)))

            return EvaluationResult(
                factuality_score   = clamp(data.get("factuality_score",   0.5)),
                coverage_score     = clamp(data.get("coverage_score",     0.5)),
                hallucination_risk = clamp(data.get("hallucination_risk", 0.5)),
                conciseness_score  = clamp(data.get("conciseness_score",  0.5)),
                issues             = [str(i) for i in data.get("issues", [])[:3]],
                raw_llm_response   = raw,
            )

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(f"[EvaluatorAgent] LLM parse failed ({exc}) — using heuristics")
            return self._heuristic_evaluate(query, answer, context_text)

    # ─────────────────────────────────────────────────────────────────────────
    #  HEURISTIC FALLBACK
    # ─────────────────────────────────────────────────────────────────────────

    def _heuristic_evaluate(
        self,
        query:        str,
        answer:       str,
        context_text: str,
    ) -> EvaluationResult:
        """
        Fast text-overlap heuristics used when LLM is unavailable.
        Not as accurate as LLM evaluation but never crashes.
        """
        issues: List[str] = []

        # --- Factuality: keyword overlap between answer and context ---
        answer_words  = set(answer.lower().split())
        context_words = set(context_text.lower().split())
        overlap_ratio = len(answer_words & context_words) / max(len(answer_words), 1)
        factuality    = float(min(1.0, overlap_ratio * 2.5))

        # --- Coverage: query keywords present in answer ---
        query_kws = [w for w in query.lower().split() if len(w) > 3]
        covered   = sum(1 for kw in query_kws if kw in answer.lower())
        coverage  = covered / max(len(query_kws), 1)

        # --- Hallucination risk: inverse factuality + length ratio ---
        answer_len    = len(answer.split())
        context_len   = len(context_text.split())
        length_ratio  = answer_len / max(context_len, 1)
        hallucination = float(max(0.0, min(1.0, (1.0 - factuality) * 0.7 + length_ratio * 0.3)))

        # --- Conciseness ---
        if answer_len < 20:
            conciseness = 0.4
            issues.append("Answer may be too brief")
        elif answer_len > 500:
            conciseness = 0.5
            issues.append("Answer may be verbose")
        else:
            conciseness = 0.8

        # --- Penalise "I don't know" responses ---
        no_info = ["don't have", "cannot find", "no information", "not available", "i'm not sure"]
        if any(p in answer.lower() for p in no_info):
            coverage  = max(0.2, coverage - 0.3)
            issues.append("Answer indicates insufficient context")

        return EvaluationResult(
            factuality_score   = round(factuality,    4),
            coverage_score     = round(coverage,      4),
            hallucination_risk = round(hallucination, 4),
            conciseness_score  = round(conciseness,   4),
            issues             = issues,
            raw_llm_response   = "heuristic_fallback",
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context_text(chunks: List[Dict[str, Any]]) -> str:
        if not chunks:
            return "No context available."
        parts = []
        for i, chunk in enumerate(chunks[:6], 1):
            content = chunk.get("content", "")[:400]
            if content:
                parts.append(f"[{i}] {content}")
        return "\n".join(parts) or "No context available."

    @staticmethod
    def _store_in_state(state: AgentState, result: EvaluationResult) -> None:
        """Persist result into state.metadata for coordinator + RL agent."""
        state.metadata["evaluation_result"] = result.to_dict()

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "name":         self.name,
            "description":  self.description,
            "capabilities": [
                "LLM-based factuality scoring",
                "Coverage assessment (query vs answer)",
                "Hallucination risk detection",
                "Conciseness scoring",
                "Heuristic fallback (no LLM required)",
                "RL reward signal generation (composite_score)",
            ],
            "output_format": "EvaluationResult (JSON-serialisable)",
            "dimensions":    ["factuality", "coverage", "hallucination_risk", "conciseness"],
            "reward_weights": {
                "factuality":    0.40,
                "coverage":      0.30,
                "hallucination": 0.20,
                "conciseness":   0.10,
            },
        }