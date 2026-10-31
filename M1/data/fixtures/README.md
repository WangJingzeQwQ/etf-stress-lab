# Phase 1 synthetic fixture policy

M1 does not contain real historical ETF data. The executable fixture is generated
at runtime by `backend/data_pipeline/phase1_fixture.py` and is always identified
as `SYNTHETIC_FIXTURE` with `real_market_evidence_allowed=false`.

The fixture has:

- a weekday-only synthetic calendar, not the Chinese exchange calendar;
- deterministic raw OHLCV and deliberately different provider qfq values;
- point-in-time total-return fields with `factor_effective_date` and
  `factor_known_at`;
- seven ETF-like codes for engineering tests;
- one explicit delisting and one historical rename interval;
- synthetic cash-dividend and split events with effective/known timestamps;
- synthetic T+0/T+1 and limit rules;
- no real historical delisting coverage or broker fee documents.

It exists to validate accounting and execution mechanics. Its returns, drawdowns,
turnover and liquidation outcomes are not empirical market evidence.

