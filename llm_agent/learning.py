"""
Reinforcement Learning Module for Autonomous Configuration Optimization

This module implements a simple Q-learning based approach to learn optimal
configuration decisions over time. The agent learns from the outcomes of
its decisions (measured by latency improvement) to make better choices.

Key Components:
1. Experience Buffer - Stores (state, action, reward) tuples
2. Q-Table - Maps state-action pairs to expected rewards
3. Reward Function - Based on latency improvement and resource efficiency

Usage:
    from learning import RLOptimizer, compute_reward
    
    optimizer = RLOptimizer()
    action = optimizer.suggest_action(state)
    optimizer.record_experience(state, action, reward)
"""

import os
import json
import logging
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Tuple, Optional
from collections import deque
from pathlib import Path
import random

logger = logging.getLogger(__name__)

# ============================================
# CONFIGURATION
# ============================================

# State discretization bins
RPS_BINS = [0, 5, 20, 100, 500, 1000, float('inf')]
LATENCY_BINS = [0, 3, 5, 8, 10, 20, float('inf')]

# Action space (max_num_seqs values)
ACTIONS = [8, 16, 32, 64, 128, 256, 512, 1024]

# RL Hyperparameters
LEARNING_RATE = float(os.getenv("RL_LEARNING_RATE", "0.1"))
DISCOUNT_FACTOR = float(os.getenv("RL_DISCOUNT_FACTOR", "0.95"))
EXPLORATION_RATE = float(os.getenv("RL_EXPLORATION_RATE", "0.1"))
EXPERIENCE_BUFFER_SIZE = int(os.getenv("RL_BUFFER_SIZE", "1000"))

# Persistence
RL_MODEL_PATH = Path(os.getenv("RL_MODEL_PATH", "./rl_model.json"))


@dataclass
class State:
    """Discretized state representation for the RL agent."""
    rps_bin: int  # Index into RPS_BINS
    latency_bin: int  # Index into LATENCY_BINS
    current_seqs_bin: int  # Index into ACTIONS (closest match)
    
    def to_tuple(self) -> Tuple[int, int, int]:
        return (self.rps_bin, self.latency_bin, self.current_seqs_bin)
    
    def to_key(self) -> str:
        return f"{self.rps_bin}_{self.latency_bin}_{self.current_seqs_bin}"


@dataclass
class Experience:
    """A single experience tuple."""
    state: State
    action_idx: int  # Index into ACTIONS
    reward: float
    next_state: Optional[State] = None
    
    def to_dict(self) -> dict:
        return {
            "state": self.state.to_tuple(),
            "action_idx": self.action_idx,
            "reward": self.reward,
            "next_state": self.next_state.to_tuple() if self.next_state else None
        }


def discretize_rps(rps: float) -> int:
    """Map RPS to a discrete bin index."""
    for i, threshold in enumerate(RPS_BINS[1:], 0):
        if rps < threshold:
            return i
    return len(RPS_BINS) - 2


def discretize_latency(latency_ms: float) -> int:
    """Map latency to a discrete bin index."""
    for i, threshold in enumerate(LATENCY_BINS[1:], 0):
        if latency_ms < threshold:
            return i
    return len(LATENCY_BINS) - 2


def discretize_seqs(seqs: int) -> int:
    """Find closest action index for a seqs value."""
    min_dist = float('inf')
    best_idx = 0
    for i, action in enumerate(ACTIONS):
        dist = abs(action - seqs)
        if dist < min_dist:
            min_dist = dist
            best_idx = i
    return best_idx


def create_state(rps: float, latency_ms: float, current_seqs: int) -> State:
    """Create a discretized state from raw metrics."""
    return State(
        rps_bin=discretize_rps(rps),
        latency_bin=discretize_latency(latency_ms),
        current_seqs_bin=discretize_seqs(current_seqs)
    )


def compute_reward(
    prev_latency_ms: float,
    new_latency_ms: float,
    target_latency_ms: float = 5.0,
    prev_rps: float = 0.0,
    new_rps: float = 0.0
) -> float:
    """
    Compute the reward for a configuration change.
    
    Uses latency-per-request-unit (latency / RPS) as the core metric.
    This way, if latency rises only because RPS increased, the agent
    is NOT penalized — it's the load that changed, not the quality
    of the decision.
    
    Returns:
        Reward value (higher is better)
    """
    # Normalize latency by load: compute "latency efficiency"
    # latency_per_rps = how much latency per unit of load
    # Lower is better — means the config handles load efficiently
    prev_rps_safe = max(prev_rps, 1.0)
    new_rps_safe = max(new_rps, 1.0)
    
    prev_efficiency = prev_latency_ms / prev_rps_safe
    new_efficiency = new_latency_ms / new_rps_safe
    
    # Efficiency improvement (positive = got better)
    if prev_efficiency > 0:
        efficiency_improvement = (prev_efficiency - new_efficiency) / prev_efficiency
    else:
        efficiency_improvement = 0.0
    
    # Scale and clamp
    latency_reward = max(-5.0, min(5.0, efficiency_improvement * 10))
    
    # Target achievement bonus (based on absolute new latency)
    target_bonus = 0.0
    if new_latency_ms <= target_latency_ms:
        target_bonus = 3.0  # Meeting target
    elif new_latency_ms <= target_latency_ms * 2:
        target_bonus = 1.0  # Close to target
    else:
        target_bonus = -1.0  # Far from target
    
    # Resource efficiency bonus (reward scaling down under low load)
    efficiency_bonus = 0.0
    if new_rps < 10 and new_latency_ms < target_latency_ms:
        efficiency_bonus = 1.0  # Efficient low-traffic handling
    
    total_reward = latency_reward + target_bonus + efficiency_bonus
    
    logger.debug(f"Reward: efficiency_imp={efficiency_improvement:.2f}, "
                 f"latency_r={latency_reward:.2f}, target={target_bonus:.2f}, "
                 f"eff_bonus={efficiency_bonus:.2f}, total={total_reward:.2f}")
    
    return total_reward


class RLOptimizer:
    """
    Q-learning based optimizer for configuration decisions.
    
    This complements the LLM-based decision making by learning from
    historical outcomes to suggest better configurations over time.
    """
    
    def __init__(self, model_path: Path = RL_MODEL_PATH):
        self.model_path = model_path
        self.q_table: Dict[str, List[float]] = {}  # state_key -> action values
        self.experience_buffer: deque = deque(maxlen=EXPERIENCE_BUFFER_SIZE)
        
        # Initialize Q-table with zeros
        self._init_q_table()
        
        # Load existing model if available
        self._load_model()
        
        logger.info(f"RL Optimizer initialized: actions={len(ACTIONS)}, "
                    f"buffer_size={EXPERIENCE_BUFFER_SIZE}")
    
    def _init_q_table(self):
        """Initialize Q-table for all possible states."""
        num_rps_bins = len(RPS_BINS) - 1
        num_lat_bins = len(LATENCY_BINS) - 1
        num_seq_bins = len(ACTIONS)
        
        for rps_bin in range(num_rps_bins):
            for lat_bin in range(num_lat_bins):
                for seq_bin in range(num_seq_bins):
                    state_key = f"{rps_bin}_{lat_bin}_{seq_bin}"
                    if state_key not in self.q_table:
                        self.q_table[state_key] = [0.0] * len(ACTIONS)
    
    def _load_model(self):
        """Load Q-table from disk if available."""
        if self.model_path.exists():
            try:
                with open(self.model_path) as f:
                    data = json.load(f)
                    self.q_table.update(data.get("q_table", {}))
                logger.info(f"Loaded RL model from {self.model_path}")
            except Exception as e:
                logger.warning(f"Could not load RL model: {e}")
    
    def save_model(self):
        """Save Q-table to disk."""
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_path, 'w') as f:
                json.dump({"q_table": self.q_table}, f, indent=2)
            logger.info(f"Saved RL model to {self.model_path}")
        except Exception as e:
            logger.warning(f"Could not save RL model: {e}")
    
    def get_q_values(self, state: State) -> List[float]:
        """Get Q-values for all actions in a given state."""
        state_key = state.to_key()
        if state_key not in self.q_table:
            self.q_table[state_key] = [0.0] * len(ACTIONS)
        return self.q_table[state_key]
    
    def suggest_action(
        self,
        rps: float,
        latency_ms: float,
        current_seqs: int,
        explore: bool = True
    ) -> int:
        """
        Suggest an optimal action (max_num_seqs value) based on learned Q-values.
        
        Args:
            rps: Current requests per second
            latency_ms: Current average latency
            current_seqs: Current max_num_seqs configuration
            explore: Whether to use epsilon-greedy exploration
            
        Returns:
            Suggested max_num_seqs value
        """
        state = create_state(rps, latency_ms, current_seqs)
        q_values = self.get_q_values(state)
        
        # Epsilon-greedy exploration
        if explore and random.random() < EXPLORATION_RATE:
            action_idx = random.randint(0, len(ACTIONS) - 1)
            logger.debug(f"RL: Exploring - random action {ACTIONS[action_idx]}")
        else:
            action_idx = q_values.index(max(q_values))
            logger.debug(f"RL: Exploiting - best action {ACTIONS[action_idx]} "
                        f"(Q={q_values[action_idx]:.2f})")
        
        return ACTIONS[action_idx]
    
    def record_experience(
        self,
        prev_rps: float,
        prev_latency_ms: float,
        prev_seqs: int,
        action_seqs: int,
        reward: float,
        new_rps: float = None,
        new_latency_ms: float = None,
        new_seqs: int = None
    ):
        """
        Record an experience and update Q-values.
        
        Args:
            prev_*: State before action
            action_seqs: Action taken (max_num_seqs value)
            reward: Computed reward
            new_*: State after action (optional, for TD learning)
        """
        state = create_state(prev_rps, prev_latency_ms, prev_seqs)
        action_idx = discretize_seqs(action_seqs)
        
        next_state = None
        if new_rps is not None and new_latency_ms is not None:
            next_state = create_state(new_rps, new_latency_ms, new_seqs or action_seqs)
        
        experience = Experience(state, action_idx, reward, next_state)
        self.experience_buffer.append(experience)
        
        # Q-learning update
        self._update_q_value(experience)
        
        # Periodically save model
        if len(self.experience_buffer) % 50 == 0:
            self.save_model()
    
    def _update_q_value(self, exp: Experience):
        """Update Q-value using TD learning."""
        state_key = exp.state.to_key()
        
        if state_key not in self.q_table:
            self.q_table[state_key] = [0.0] * len(ACTIONS)
        
        current_q = self.q_table[state_key][exp.action_idx]
        
        # Compute TD target
        if exp.next_state:
            next_q_values = self.get_q_values(exp.next_state)
            max_next_q = max(next_q_values)
            td_target = exp.reward + DISCOUNT_FACTOR * max_next_q
        else:
            td_target = exp.reward
        
        # Q-learning update
        new_q = current_q + LEARNING_RATE * (td_target - current_q)
        self.q_table[state_key][exp.action_idx] = new_q
        
        logger.debug(f"Q-update: state={state_key}, action={exp.action_idx}, "
                    f"old_q={current_q:.2f}, new_q={new_q:.2f}")
    
    def get_statistics(self) -> dict:
        """Get statistics about the RL optimizer."""
        return {
            "buffer_size": len(self.experience_buffer),
            "q_table_size": len(self.q_table),
            "total_states": (len(RPS_BINS) - 1) * (len(LATENCY_BINS) - 1) * len(ACTIONS),
            "actions": ACTIONS,
            "exploration_rate": EXPLORATION_RATE,
        }


# Global optimizer instance (lazy initialization)
_optimizer: Optional[RLOptimizer] = None


def get_optimizer() -> RLOptimizer:
    """Get or create the global RL optimizer instance."""
    global _optimizer
    if _optimizer is None:
        _optimizer = RLOptimizer()
    return _optimizer
