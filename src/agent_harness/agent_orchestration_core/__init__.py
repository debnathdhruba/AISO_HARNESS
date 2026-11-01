"""Preplanned, deterministic orchestration for bounded subagent execution."""

from agent_harness.agent_orchestration_core.paging import ContextPager, PageFrame
from agent_harness.agent_orchestration_core.planning import PlanCompiler
from agent_harness.agent_orchestration_core.runtime import KernelPlanRuntime

__all__ = (
    "AutomaticPlan",
    "ContextPager",
    "KernelMemory",
    "KernelPlanRuntime",
    "PageFrame",
    "PlanCompiler",
    "decompose",
    "should_spawn",
)
from agent_harness.agent_orchestration_core.automatic import AutomaticPlan, decompose, should_spawn
from agent_harness.memory.short_term import KernelMemory
