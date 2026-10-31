# Synthetic fixture policy

M0 does not contain real historical ETF data. The executable fixture is generated
at runtime by `backend/data_pipeline/fixture.py` and is always identified as
`SYNTHETIC_FIXTURE`.

The fixture has:

- a weekday-only synthetic calendar, not the Chinese exchange calendar;
- deterministic raw OHLCV and deliberately different provider qfq values;
- point-in-time total-return fields with `factor_effective_date` and
  `factor_known_at`;
- synthetic Chinese names and six ETF-like codes for engineering tests;
- no historical delisting coverage or real corporate-action documents.

It exists to validate accounting and execution mechanics. Its returns, drawdowns,
turnover and liquidation outcomes are not empirical market evidence.

