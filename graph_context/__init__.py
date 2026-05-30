from graph_context.engine import VibeCodingEngine, DEFAULT_CONFIG
from graph_context.rules import Rule, RulesStore
from graph_context.project_scope import ProjectScope, ScopeManager, PatternSummary, AggregatedPattern

__version__ = "6.2.0"
__all__ = [
    "VibeCodingEngine", "DEFAULT_CONFIG",
    "Rule", "RulesStore",
    "ProjectScope", "ScopeManager", "PatternSummary", "AggregatedPattern",
]


def main():
    from graph_context.server import main as _main
    _main()
