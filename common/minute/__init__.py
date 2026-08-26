"""Market-neutral minute-execution machinery shared by futures strategies.

The modules here were extracted from ``cta_carry`` once a second consumer
appeared. They must stay free of any single strategy's vocabulary: a market's
particulars enter through a :class:`~common.minute.sessions.SessionRuleset`,
never through a module-level constant.
"""
