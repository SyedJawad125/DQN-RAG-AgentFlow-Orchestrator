"""
RL Memory Manager — v2
-----------------------
Replaces the tabular Q-table from v1 with a Deep Q-Network (DQN).

Key upgrades vs v1:
    1. DQN (2-layer MLP, pure numpy — zero new dependencies)
    2. Target network — copy of online DQN, updated every 50 steps
       Prevents the oscillating Q-values that hurt tabular training
    3. 6-dimensional continuous state vector (v1 had 4 discrete buckets)
    4. Batch training via experience replay (same buffer, better update)
    5. WarmStartTrainer — pre-trains DQN on 800 synthetic (state, action)
       pairs before epsilon-greedy exploration begins, eliminating the
       cold-start random phase that gave wrong answers for the first ~50 queries
    6. Legacy QTable kept for backward-compatible checkpoint loading

New 6D state vector (all normalised to [0, 1]):
    [0] confidence        — retrieval relevance score from RAGAgent
    [1] retrieval_density — n_chunks / 15 (MAX_CHUNKS)
    [2] complexity        — 0.0 / 0.5 / 1.0  (simple / medium / complex)
    [3] has_internet      — 0.0 or 1.0
    [4] source_diversity  — unique source files / total chunks
    [5] step_ratio        — steps_taken / MAX_STEPS (5)

Action space (unchanged from v1):
    0 → RETRIEVE_MORE
    1 → RE_RANK
    2 → ANSWER_NOW
    3 → ASK_CLARIFICATION

Files written to disk:
    rl_qtable.json      — legacy Q-table (backward compat)
    rl_qtable_dqn.json  — DQN weights + target weights + epsilon
"""

import json
import os
import random
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  ACTION MAP  (unchanged from v1 — coordinator still uses ACTIONS / ACTION_IDX)
# ─────────────────────────────────────────────────────────────────────────────

ACTIONS: Dict[int, str] = {
    0: "RETRIEVE_MORE",
    1: "RE_RANK",
    2: "ANSWER_NOW",
    3: "ASK_CLARIFICATION",
}

ACTION_IDX: Dict[str, int] = {v: k for k, v in ACTIONS.items()}

STATE_DIM = 6   # continuous state dimensions
N_ACTIONS = 4
MAX_CHUNKS = 15  # used to normalise retrieval_density


# ─────────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RLExperience:
    """Single (s, a, r, s', done) tuple stored in the replay buffer."""
    state:      np.ndarray   # shape (STATE_DIM,)
    action:     int
    reward:     float
    next_state: np.ndarray   # shape (STATE_DIM,)
    done:       bool
    query_id:   str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  DQN NETWORK — pure numpy, no PyTorch / TensorFlow required
# ─────────────────────────────────────────────────────────────────────────────

class DQNNetwork:
    """
    2-layer MLP:  STATE_DIM(6) → 32 → 32 → N_ACTIONS(4)

    Trained with mini-batch SGD (Huber-ish MSE loss).
    Xavier weight initialisation.
    Gradient clipping (global norm ≤ 1.0) for stability.

    No external ML library needed.
    """

    def __init__(
        self,
        state_dim: int   = STATE_DIM,
        n_actions: int   = N_ACTIONS,
        hidden:    int   = 32,
        lr:        float = 0.001,
    ) -> None:
        self.state_dim = state_dim
        self.n_actions = n_actions
        self.hidden    = hidden
        self.lr        = lr
        self._loss_buf: List[float] = []
        self._init_weights()

    # ── Xavier initialisation ─────────────────────────────────────────────

    def _init_weights(self) -> None:
        rng  = np.random.default_rng(42)
        lim1 = np.sqrt(6.0 / (self.state_dim + self.hidden))
        lim2 = np.sqrt(6.0 / (self.hidden     + self.hidden))
        lim3 = np.sqrt(6.0 / (self.hidden     + self.n_actions))

        self.W1 = rng.uniform(-lim1, lim1, (self.state_dim, self.hidden))
        self.b1 = np.zeros(self.hidden)
        self.W2 = rng.uniform(-lim2, lim2, (self.hidden, self.hidden))
        self.b2 = np.zeros(self.hidden)
        self.W3 = rng.uniform(-lim3, lim3, (self.hidden, self.n_actions))
        self.b3 = np.zeros(self.n_actions)

    # ── Forward pass (stores intermediates for backprop) ─────────────────

    def forward(self, x: np.ndarray) -> np.ndarray:
        """x: (batch, state_dim) → q: (batch, n_actions)"""
        x        = np.atleast_2d(x)
        self._x  = x
        self._z1 = x @ self.W1 + self.b1
        self._a1 = np.maximum(0.0, self._z1)      # ReLU
        self._z2 = self._a1 @ self.W2 + self.b2
        self._a2 = np.maximum(0.0, self._z2)      # ReLU
        self._z3 = self._a2 @ self.W3 + self.b3
        return self._z3                            # raw Q-values

    def predict(self, state: np.ndarray) -> np.ndarray:
        """Single state → 1D Q-values (no grad storage, safe for target net)."""
        x  = np.atleast_2d(state)
        a1 = np.maximum(0.0, x  @ self.W1 + self.b1)
        a2 = np.maximum(0.0, a1 @ self.W2 + self.b2)
        return (a2 @ self.W3 + self.b3)[0]

    # ── Backward pass ────────────────────────────────────────────────────

    def train_batch(
        self,
        states:  np.ndarray,   # (B, STATE_DIM)
        actions: np.ndarray,   # (B,)  int
        targets: np.ndarray,   # (B,)  float — TD targets
    ) -> float:
        """
        One SGD step on a batch.
        Only updates Q(s, a) for the taken action — other actions frozen.
        Returns mean squared error.
        """
        B   = len(states)
        q   = self.forward(states)              # (B, N_ACTIONS)
        idx = np.arange(B)

        errors              = q[idx, actions] - targets   # (B,)
        delta               = np.zeros_like(q)
        delta[idx, actions] = errors

        # Layer 3
        dz3 = delta / B
        dW3 = self._a2.T @ dz3
        db3 = dz3.sum(0)

        # Layer 2
        da2 = dz3 @ self.W3.T
        dz2 = da2 * (self._z2 > 0)
        dW2 = self._a1.T @ dz2
        db2 = dz2.sum(0)

        # Layer 1
        da1 = dz2 @ self.W2.T
        dz1 = da1 * (self._z1 > 0)
        dW1 = self._x.T @ dz1
        db1 = dz1.sum(0)

        # Global gradient clipping
        all_grads = [dW3, db3, dW2, db2, dW1, db1]
        total_norm = np.sqrt(sum(np.sum(g ** 2) for g in all_grads))
        if total_norm > 1.0:
            clip = 1.0 / total_norm
            all_grads = [g * clip for g in all_grads]
        dW3, db3, dW2, db2, dW1, db1 = all_grads

        # SGD update
        self.W3 -= self.lr * dW3;  self.b3 -= self.lr * db3
        self.W2 -= self.lr * dW2;  self.b2 -= self.lr * db2
        self.W1 -= self.lr * dW1;  self.b1 -= self.lr * db1

        loss = float(0.5 * np.mean(errors ** 2))
        self._loss_buf.append(loss)
        return loss

    # ── Persistence ───────────────────────────────────────────────────────

    def get_weights(self) -> Dict[str, list]:
        return {
            "W1": self.W1.tolist(), "b1": self.b1.tolist(),
            "W2": self.W2.tolist(), "b2": self.b2.tolist(),
            "W3": self.W3.tolist(), "b3": self.b3.tolist(),
        }

    def set_weights(self, d: Dict[str, list]) -> None:
        self.W1 = np.array(d["W1"]); self.b1 = np.array(d["b1"])
        self.W2 = np.array(d["W2"]); self.b2 = np.array(d["b2"])
        self.W3 = np.array(d["W3"]); self.b3 = np.array(d["b3"])

    def copy_from(self, other: "DQNNetwork") -> None:
        """Hard copy of all weights (used for target network sync)."""
        self.W1 = other.W1.copy(); self.b1 = other.b1.copy()
        self.W2 = other.W2.copy(); self.b2 = other.b2.copy()
        self.W3 = other.W3.copy(); self.b3 = other.b3.copy()

    @property
    def recent_loss(self) -> float:
        if not self._loss_buf:
            return 0.0
        return float(np.mean(self._loss_buf[-50:]))


# ─────────────────────────────────────────────────────────────────────────────
#  EXPERIENCE REPLAY BUFFER
# ─────────────────────────────────────────────────────────────────────────────

class ExperienceReplayBuffer:
    """Fixed-capacity circular buffer — O(1) push, O(k) random sample."""

    def __init__(self, capacity: int = 10_000) -> None:
        self._buf: deque = deque(maxlen=capacity)

    def push(self, exp: RLExperience) -> None:
        self._buf.append(exp)

    def sample(self, k: int) -> List[RLExperience]:
        return random.sample(self._buf, min(k, len(self._buf)))

    def sample_arrays(self, k: int):
        """Return sampled experiences as stacked numpy arrays for batch training."""
        batch   = self.sample(k)
        states  = np.stack([e.state      for e in batch])
        actions = np.array([e.action     for e in batch])
        rewards = np.array([e.reward     for e in batch])
        nexts   = np.stack([e.next_state for e in batch])
        dones   = np.array([float(e.done) for e in batch])
        return states, actions, rewards, nexts, dones

    def __len__(self) -> int:
        return len(self._buf)


# ─────────────────────────────────────────────────────────────────────────────
#  WARM-START TRAINER
# ─────────────────────────────────────────────────────────────────────────────

class WarmStartTrainer:
    """
    Pre-trains the DQN on synthetic (state, optimal_action) pairs
    BEFORE epsilon-greedy exploration begins.

    Without warm-start: first ~50–100 queries get random or wrong actions
    because Q-values are uninitialised, causing unnecessary RETRIEVE_MORE
    calls and poor early rewards.

    With warm-start: agent starts with a sensible prior:
        high confidence   → ANSWER_NOW
        low conf + no chunks → RETRIEVE_MORE
        many chunks, medium conf → RE_RANK
        near max steps    → ANSWER_NOW (force termination)

    Called once on first boot (if no DQN checkpoint exists).
    """

    @staticmethod
    def generate_pairs(n: int = 800) -> List[Tuple[np.ndarray, int]]:
        rng   = np.random.default_rng(0)
        pairs = []

        for _ in range(n):
            conf      = rng.uniform(0.0, 1.0)   # confidence
            retr      = rng.uniform(0.0, 1.0)   # retrieval_density
            comp      = rng.choice([0.0, 0.5, 1.0])
            internet  = rng.choice([0.0, 1.0])
            diversity = rng.uniform(0.0, 1.0)
            step_r    = rng.uniform(0.0, 1.0)

            state = np.array([conf, retr, comp, internet, diversity, step_r])

            # Domain-knowledge rules for optimal action
            if conf >= 0.72:
                action = ACTION_IDX["ANSWER_NOW"]
            elif conf < 0.35 and retr < 0.20:
                action = ACTION_IDX["RETRIEVE_MORE"]
            elif conf >= 0.45 and retr >= 0.35:
                action = ACTION_IDX["RE_RANK"]
            elif step_r >= 0.80:
                action = ACTION_IDX["ANSWER_NOW"]   # near MAX_STEPS → answer
            else:
                action = ACTION_IDX["RETRIEVE_MORE"]

            pairs.append((state, action))

        return pairs

    @staticmethod
    def pretrain(net: DQNNetwork, n_pairs: int = 800, epochs: int = 5) -> None:
        """Supervised warm-start: treat optimal_action as a hard Q-target (0.9)."""
        pairs = WarmStartTrainer.generate_pairs(n_pairs)
        logger.info(f"[WarmStart] Pre-training DQN on {n_pairs} pairs × {epochs} epochs")

        for epoch in range(epochs):
            random.shuffle(pairs)
            epoch_losses: List[float] = []
            batch_size = 64

            for i in range(0, len(pairs), batch_size):
                batch   = pairs[i : i + batch_size]
                states  = np.stack([s for s, _ in batch])
                actions = np.array([a for _, a in batch])

                # Q-targets: 0.9 for correct action, 0.0333 for others (soft label)
                q_targets = net.forward(states).copy()
                for j, (_, a) in enumerate(batch):
                    q_targets[j, :] = 0.0333
                    q_targets[j, a] = 0.90

                targets = q_targets[np.arange(len(batch)), actions]
                loss    = net.train_batch(states, actions, targets)
                epoch_losses.append(loss)

            avg_loss = np.mean(epoch_losses)
            logger.info(f"[WarmStart] Epoch {epoch + 1}/{epochs} — avg_loss={avg_loss:.4f}")

        logger.info("[WarmStart] Pre-training complete")


# ─────────────────────────────────────────────────────────────────────────────
#  LEGACY Q-TABLE  (kept for backward-compatible checkpoint loading)
# ─────────────────────────────────────────────────────────────────────────────

class QTable:
    """
    Tabular Q-learning from v1.  Still used as:
        - Emergency fallback if DQN is unavailable
        - Source of initial epsilon value from disk checkpoint
    API is identical to v1 so nothing else breaks.
    """

    def __init__(
        self,
        n_actions:     int   = N_ACTIONS,
        learning_rate: float = 0.1,
        discount:      float = 0.9,
        epsilon:       float = 0.3,
        epsilon_min:   float = 0.05,
        epsilon_decay: float = 0.995,
    ) -> None:
        self.n_actions     = n_actions
        self.lr            = learning_rate
        self.gamma         = discount
        self.epsilon       = epsilon
        self.epsilon_min   = epsilon_min
        self.epsilon_decay = epsilon_decay
        self.table:        Dict[str, List[float]] = {}
        self.total_updates: int = 0

    @staticmethod
    def _key(state) -> str:
        return str(state)

    def q_values(self, state) -> List[float]:
        key = self._key(state)
        if key not in self.table:
            self.table[key] = [0.5, 0.3, 0.7, 0.2]   # optimistic init
        return self.table[key]

    def best_action(self, state) -> int:
        return int(np.argmax(self.q_values(state)))

    def select_action(self, state, training: bool = True) -> int:
        if training and random.random() < self.epsilon:
            return random.randrange(self.n_actions)
        return self.best_action(state)

    def update(self, state, action: int, reward: float, next_state, done: bool) -> None:
        q_sa    = self.q_values(state)[action]
        target  = reward if done else reward + self.gamma * max(self.q_values(next_state))
        q       = self.q_values(state)
        q[action] = q_sa + self.lr * (target - q_sa)
        self.table[self._key(state)] = q
        self.epsilon       = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        self.total_updates += 1

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"table": self.table, "epsilon": self.epsilon,
                       "total_updates": self.total_updates}, f, indent=2)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path) as f:
            d = json.load(f)
        self.table         = d.get("table", {})
        self.epsilon       = d.get("epsilon", self.epsilon)
        self.total_updates = d.get("total_updates", 0)


# ─────────────────────────────────────────────────────────────────────────────
#  SINGLETON MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class RLMemoryManager:
    """
    Process-level singleton that owns:
        - DQN online network        (trained every step)
        - DQN target network        (hard-copied every TARGET_UPDATE_FREQ steps)
        - ExperienceReplayBuffer    (capacity 10 k)
        - Legacy QTable             (fallback + checkpoint compat)

    Backward compatible with v1 — RLDecisionAgent v2 uses the same interface.
    """

    TARGET_UPDATE_FREQ = 50   # copy online → target every N DQN updates

    _instance: Optional["RLMemoryManager"] = None

    def __new__(cls) -> "RLMemoryManager":
        if cls._instance is None:
            obj = super().__new__(cls)
            obj._ready = False
            cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if self._ready:
            return

        from django.conf import settings

        qtable_path         = str(
            getattr(settings, "RL_QTABLE_PATH",
                    Path(getattr(settings, "BASE_DIR", ".")) / "rl_qtable.json")
        )
        self.checkpoint_path = qtable_path
        self.dqn_path        = qtable_path.replace(".json", "_dqn.json")

        lr            = float(getattr(settings, "RL_LEARNING_RATE", 0.001))
        self.gamma    = float(getattr(settings, "RL_DISCOUNT",       0.9))
        self.epsilon  = float(getattr(settings, "RL_EPSILON",        0.3))
        self.eps_min  = float(getattr(settings, "RL_EPSILON_MIN",   0.05))
        self.eps_dec  = float(getattr(settings, "RL_EPSILON_DECAY", 0.995))

        # DQN — online + target
        self.dqn        = DQNNetwork(STATE_DIM, N_ACTIONS, hidden=32, lr=lr)
        self.target_net = DQNNetwork(STATE_DIM, N_ACTIONS, hidden=32, lr=lr)

        # Replay buffer
        cap             = int(getattr(settings, "RL_REPLAY_CAPACITY", 10_000))
        self.replay_buf = ExperienceReplayBuffer(capacity=cap)

        # Update counter
        self.total_updates = 0

        # Legacy Q-table
        self.q_table = QTable(
            n_actions=N_ACTIONS, learning_rate=lr,
            discount=self.gamma, epsilon=self.epsilon,
        )
        self.q_table.load(self.checkpoint_path)
        # Inherit epsilon from legacy checkpoint if newer DQN checkpoint absent
        self.epsilon = self.q_table.epsilon

        # Load DQN or warm-start
        if not self._load_dqn():
            WarmStartTrainer.pretrain(self.dqn, n_pairs=800, epochs=5)
            self.target_net.copy_from(self.dqn)
            self._save_dqn()

        self._ready = True
        logger.info(
            f"[RLMemoryManager v2] Initialized | ε={self.epsilon:.4f} | "
            f"updates={self.total_updates}"
        )

    # ── Action selection ─────────────────────────────────────────────────

    def select_action(self, state: np.ndarray, training: bool = True) -> int:
        """Epsilon-greedy selection using online DQN."""
        if training and random.random() < self.epsilon:
            return random.randrange(N_ACTIONS)
        q = self.dqn.predict(state)
        return int(np.argmax(q))

    def q_values_for_state(self, state: np.ndarray) -> np.ndarray:
        """Return Q-values for a state (for logging / API stats)."""
        return self.dqn.predict(state)

    # ── DQN update ───────────────────────────────────────────────────────

    def update_dqn(
        self,
        state:      np.ndarray,
        action:     int,
        reward:     float,
        next_state: np.ndarray,
        done:       bool,
    ) -> None:
        """Single-step DQN update using target network for TD target."""
        if done:
            target = reward
        else:
            next_q  = self.target_net.predict(next_state)
            target  = reward + self.gamma * float(np.max(next_q))

        self.dqn.train_batch(
            np.atleast_2d(state),
            np.array([action]),
            np.array([target]),
        )

        # Epsilon decay
        self.epsilon       = max(self.eps_min, self.epsilon * self.eps_dec)
        self.total_updates += 1

        # Periodic hard copy: online → target
        if self.total_updates % self.TARGET_UPDATE_FREQ == 0:
            self.target_net.copy_from(self.dqn)
            logger.info(
                f"[RLMemoryManager] Target network synced at update {self.total_updates}"
            )

    # ── Replay training ───────────────────────────────────────────────────

    def replay_train(self, batch_size: int = 32) -> None:
        """Sample from replay buffer and run one DQN batch-update step."""
        if len(self.replay_buf) < batch_size:
            return

        states, actions, rewards, nexts, dones = self.replay_buf.sample_arrays(batch_size)

        # TD targets via target network (Double-DQN style)
        next_q  = np.stack([self.target_net.predict(s) for s in nexts])  # (B, N_ACTIONS)
        targets = rewards + self.gamma * next_q.max(axis=1) * (1.0 - dones)

        loss = self.dqn.train_batch(states, actions, targets)

        if self.total_updates % self.TARGET_UPDATE_FREQ == 0 and self.total_updates > 0:
            self.target_net.copy_from(self.dqn)

        logger.debug(f"[RLMemoryManager] Replay train — loss={loss:.4f}")

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self) -> None:
        self._save_dqn()
        self.q_table.epsilon = self.epsilon           # keep legacy in sync
        self.q_table.save(self.checkpoint_path)

    def _save_dqn(self) -> None:
        try:
            parent = os.path.dirname(self.dqn_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.dqn_path, "w") as f:
                json.dump({
                    "online":        self.dqn.get_weights(),
                    "target":        self.target_net.get_weights(),
                    "epsilon":       self.epsilon,
                    "total_updates": self.total_updates,
                }, f)
            logger.info(f"[RLMemoryManager] DQN saved → {self.dqn_path}")
        except Exception as exc:
            logger.warning(f"[RLMemoryManager] DQN save failed: {exc}")

    def _load_dqn(self) -> bool:
        if not os.path.exists(self.dqn_path):
            return False
        try:
            with open(self.dqn_path) as f:
                d = json.load(f)
            self.dqn.set_weights(d["online"])
            self.target_net.set_weights(d["target"])
            self.epsilon       = d.get("epsilon",       self.epsilon)
            self.total_updates = d.get("total_updates", 0)
            logger.info(
                f"[RLMemoryManager] DQN loaded from {self.dqn_path} | "
                f"ε={self.epsilon:.4f} | updates={self.total_updates}"
            )
            return True
        except Exception as exc:
            logger.warning(f"[RLMemoryManager] DQN load failed: {exc}")
            return False

    @property
    def recent_loss(self) -> float:
        return self.dqn.recent_loss