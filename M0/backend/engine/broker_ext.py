from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from backend.data_pipeline.models import CorporateAction, MarketBar, MarketDataset
from backend.engine.account import AccountState, InterestCharge
from backend.engine.cost_model import CostModel, money_round
from backend.engine.execution import ExecutionModel, ExecutionQuote
from backend.engine.liquidation import (
    LiquidationSettlement,
    liquidation_line,
    settle_liquidation_proceeds,
)
from backend.engine.models import (
    AccountStatus,
    LiquidationSummary,
    Position,
    TradeExecution,
    ZERO,
    decimal_value,
)
from backend.engine.snapshot import Snapshot, build_snapshot
from backend.engine.target_portfolio import (
    PortfolioValidationError,
    TargetPortfolio,
    validate_target_portfolio,
)


@dataclass(frozen=True)
class BrokerConfig:
    initial_cash: Decimal
    leverage: Decimal
    capital_base_mode: str
    financing_mode: str
    reentry_policy: str
    annual_interest_rate: Decimal
    interest_day_count: int
    maintenance_fraction: Decimal
    maintenance_floor: Decimal
    lot_size: int
    p0_1_frozen: bool
    liquidation_model: str

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "BrokerConfig":
        required = {
            "initial_cash",
            "leverage",
            "capital_base_mode",
            "financing_mode",
            "reentry_policy",
            "annual_interest_rate",
            "interest_day_count",
            "maintenance_fraction",
            "maintenance_floor",
            "lot_size",
            "p0_1_frozen",
            "liquidation_model",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"Broker configuration missing fields: {missing}")
        config = cls(
            initial_cash=decimal_value(payload["initial_cash"]),
            leverage=decimal_value(payload["leverage"]),
            capital_base_mode=str(payload["capital_base_mode"]),
            financing_mode=str(payload["financing_mode"]),
            reentry_policy=str(payload["reentry_policy"]),
            annual_interest_rate=decimal_value(payload["annual_interest_rate"]),
            interest_day_count=int(payload["interest_day_count"]),
            maintenance_fraction=decimal_value(payload["maintenance_fraction"]),
            maintenance_floor=decimal_value(payload["maintenance_floor"]),
            lot_size=int(payload["lot_size"]),
            p0_1_frozen=bool(payload["p0_1_frozen"]),
            liquidation_model=str(payload["liquidation_model"]),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.initial_cash <= ZERO:
            raise ValueError("initial_cash must be positive")
        if self.leverage < Decimal("1"):
            raise ValueError("leverage must be at least 1")
        if self.capital_base_mode not in {"initial_equity", "current_equity"}:
            raise ValueError("Unsupported capital_base_mode")
        if self.financing_mode != "synthetic_stress":
            raise ValueError("M0 supports financing_mode=synthetic_stress only")
        if self.reentry_policy != "first_liquidation_terminates":
            raise ValueError(
                "M0 main survival path requires first_liquidation_terminates"
            )
        if self.annual_interest_rate < ZERO:
            raise ValueError("annual_interest_rate cannot be negative")
        if self.interest_day_count <= 0:
            raise ValueError("interest_day_count must be positive")
        if self.maintenance_fraction < ZERO or self.maintenance_floor < ZERO:
            raise ValueError("Maintenance parameters cannot be negative")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.liquidation_model != "synthetic_v1" or not self.p0_1_frozen:
            raise ValueError(
                "M0 requires liquidation_model=synthetic_v1 and p0_1_frozen=true"
            )


@dataclass(frozen=True)
class TargetLine:
    code: str
    name: str
    weight: float
    pv_target: Decimal
    target_shares: int
    pv_actual: Decimal
    target_deviation: Decimal
    filled_buy_shares: int = 0
    filled_sell_shares: int = 0
    unfilled_shares: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "weight": self.weight,
            "pv_target": self.pv_target,
            "target_shares": self.target_shares,
            "pv_actual": self.pv_actual,
            "target_deviation": self.target_deviation,
            "filled_buy_shares": self.filled_buy_shares,
            "filled_sell_shares": self.filled_sell_shares,
            "unfilled_shares": self.unfilled_shares,
        }


@dataclass(frozen=True)
class TargetExecutionSummary:
    signal_date: date
    execution_date: date
    total_r: float
    l_entry: Decimal
    capital_base: Decimal
    buying_power: Decimal
    own_capital_used: Decimal
    credit_limit: Decimal
    pv_target_total: Decimal
    pv_actual_total: Decimal
    lines: tuple[TargetLine, ...]
    rejected: bool = False
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_date": self.signal_date,
            "execution_date": self.execution_date,
            "total_r": self.total_r,
            "l_entry": self.l_entry,
            "capital_base": self.capital_base,
            "buying_power": self.buying_power,
            "own_capital_used": self.own_capital_used,
            "credit_limit": self.credit_limit,
            "pv_target_total": self.pv_target_total,
            "pv_actual_total": self.pv_actual_total,
            "pv_target_deviation": self.pv_target_total - self.pv_actual_total,
            "lines": [line.to_dict() for line in self.lines],
            "rejected": self.rejected,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class DayContext:
    trading_date: date
    next_session: date | None
    daily_interest: InterestCharge
    can_rebalance: bool
    liquidation_triggered: bool


@dataclass(frozen=True)
class DayResult:
    trading_date: date
    snapshot: Snapshot
    trades: tuple[TradeExecution, ...]
    liquidation: LiquidationSummary | None
    target_summary: TargetExecutionSummary | None
    interest: InterestCharge
    rejection_reason: str | None = None


@dataclass
class _TradePlan:
    code: str
    name: str
    target_shares: int
    pv_target: Decimal
    pv_actual: Decimal
    target_deviation: Decimal
    weight: float
    filled_buy: int = 0
    filled_sell: int = 0
    unfilled: int = 0

    def as_line(self) -> TargetLine:
        return TargetLine(
            code=self.code,
            name=self.name,
            weight=self.weight,
            pv_target=self.pv_target,
            target_shares=self.target_shares,
            pv_actual=self.pv_actual,
            target_deviation=self.target_deviation,
            filled_buy_shares=self.filled_buy,
            filled_sell_shares=self.filled_sell,
            unfilled_shares=self.unfilled,
        )


class Broker:
    """Single authority for cash, debt, holdings, execution, and liquidation."""

    def __init__(
        self,
        config: BrokerConfig,
        cost_model: CostModel,
        execution_model: ExecutionModel,
    ) -> None:
        self.config = config
        self.cost_model = cost_model
        self.execution_model = execution_model
        self.account = AccountState.open(config.initial_cash)
        self.last_l_entry = Decimal("1")
        self.previous_close_equity: Decimal | None = None
        self.target_summaries: list[TargetExecutionSummary] = []
        self.rejected_targets: list[dict[str, Any]] = []
        self.liquidations: list[LiquidationSummary] = []
        self._current_date: date | None = None
        self._current_dataset: MarketDataset | None = None
        self._current_context: DayContext | None = None
        self._current_trades: list[TradeExecution] = []
        self._current_liquidation: LiquidationSummary | None = None
        self._current_target_summary: TargetExecutionSummary | None = None
        self._active_target_for_trades: TargetPortfolio | None = None
        self._rejection_reason: str | None = None
        self._day_counter = 0

    def begin_day(
        self, dataset: MarketDataset, trading_date: date
    ) -> DayContext:
        if self._current_date is not None:
            raise RuntimeError("Previous trading day has not been finalized")
        self._current_date = trading_date
        self._current_dataset = dataset
        self._current_trades = []
        self._current_liquidation = None
        self._current_target_summary = None
        self._active_target_for_trades = None
        self._rejection_reason = None
        self._day_counter += 1
        self.cost_model.assert_effective(trading_date)
        next_session = next(
            (session for session in dataset.sessions if session > trading_date), None
        )

        self.account.unlock_at_start_of(trading_date)
        daily_interest = self.account.apply_interest(
            trading_date,
            self.config.annual_interest_rate,
            self.config.interest_day_count,
        )
        self._apply_corporate_actions(dataset, trading_date)

        open_bars = dataset.session_bars(trading_date)
        open_prices = {
            code: bar.raw_open for code, bar in open_bars.items()
        }
        for code, price in open_prices.items():
            self.account.mark_price(code, price)

        triggered = False
        if not self.account.status.is_terminal and self.account.positions:
            if self.account.status == AccountStatus.FORCED_LIQUIDATION:
                triggered = True
            elif (
                self.account.debt > ZERO
                and self.account.margin_ratio(open_prices) is not None
                and self.account.margin_ratio(open_prices)
                < self.config_liquidation_line()
            ):
                triggered = True
        if triggered:
            self._liquidation(
                dataset,
                trading_date,
                open_bars,
                open_prices,
            )
        context = DayContext(
            trading_date=trading_date,
            next_session=next_session,
            daily_interest=daily_interest,
            can_rebalance=(
                self.account.status == AccountStatus.ACTIVE
                and not triggered
                and not self.account.status.is_terminal
            ),
            liquidation_triggered=triggered,
        )
        self._current_context = context
        return context

    def config_liquidation_line(self) -> Decimal:
        return liquidation_line(
            self.last_l_entry,
            maintenance_fraction=self.config.maintenance_fraction,
            maintenance_floor=self.config.maintenance_floor,
        )

    def execute_target(
        self,
        dataset: MarketDataset,
        target: TargetPortfolio,
        allowed_codes: set[str],
        trading_date: date | None = None,
    ) -> TargetExecutionSummary | None:
        if self._current_date is None or self._current_context is None:
            raise RuntimeError("begin_day must be called before execute_target")
        current_date = trading_date or self._current_date
        if current_date != self._current_date:
            raise ValueError("Target date does not match the active trading day")
        if self.account.status.is_terminal:
            self._reject_target(
                current_date,
                target,
                [f"account is terminal: {self.account.status.value}"],
            )
            return None
        if not self._current_context.can_rebalance:
            self._reject_target(
                current_date,
                target,
                ["strategy rebalance skipped during forced liquidation"],
            )
            return None
        self._active_target_for_trades = target
        try:
            validate_target_portfolio(
                target,
                allowed_codes,
                expected_execution_date=current_date,
            )
            self._validate_bars_and_rules(dataset, current_date, target)
        except PortfolioValidationError as exc:
            self._reject_target(current_date, target, list(exc.errors))
            self._active_target_for_trades = None
            return None

        capital_base = self._capital_base()
        credit_limit = max(
            ZERO, (self.config.leverage - Decimal("1")) * capital_base
        )
        pv_target_total = (
            decimal_value(target.total_r) * capital_base * self.config.leverage
        )
        buying_power = capital_base * self.config.leverage
        own_capital_used = min(capital_base, pv_target_total)
        target_l_entry = decimal_value(target.total_r) * self.config.leverage
        if target_l_entry > ZERO:
            self.last_l_entry = target_l_entry
        bars = dataset.session_bars(current_date)
        plans: dict[str, _TradePlan] = {}
        target_codes = set(target.weights)
        all_codes = target_codes | set(self.account.positions)
        for code in sorted(all_codes):
            bar = bars.get(code)
            if bar is None:
                raise AssertionError(f"Missing bar for {code} after validation")
            weight = float(target.weights.get(code, 0.0))
            pv_target = pv_target_total * decimal_value(weight)
            lots = (
                pv_target / bar.raw_open / decimal_value(self.config.lot_size)
            ).to_integral_value(rounding=ROUND_FLOOR)
            target_shares = int(lots) * self.config.lot_size
            pv_actual = decimal_value(target_shares) * bar.raw_open
            plans[code] = _TradePlan(
                code=code,
                name=bar.name,
                target_shares=target_shares,
                pv_target=pv_target,
                pv_actual=pv_actual,
                target_deviation=pv_target - pv_actual,
                weight=weight,
            )

        for code in sorted(plans):
            self._active_sell(
                dataset,
                current_date,
                plans[code],
                bars[code],
            )
        for code in sorted(plans):
            self._active_buy(
                dataset,
                current_date,
                plans[code],
                bars[code],
                credit_limit,
            )

        pv_actual_total = sum(
            (plan.pv_actual for plan in plans.values()), ZERO
        )
        summary = TargetExecutionSummary(
            signal_date=target.signal_date,
            execution_date=current_date,
            total_r=target.total_r,
            l_entry=self.last_l_entry,
            capital_base=capital_base,
            buying_power=buying_power,
            own_capital_used=own_capital_used,
            credit_limit=credit_limit,
            pv_target_total=pv_target_total,
            pv_actual_total=pv_actual_total,
            lines=tuple(plans[code].as_line() for code in sorted(plans)),
        )
        self.target_summaries.append(summary)
        self._current_target_summary = summary
        self._active_target_for_trades = None
        return summary

    def finish_day(self, dataset: MarketDataset) -> DayResult:
        if self._current_date is None or self._current_context is None:
            raise RuntimeError("begin_day must be called before finish_day")
        trading_date = self._current_date
        bars = dataset.session_bars(trading_date)
        close_prices: dict[str, Decimal] = {}
        for code, position in self.account.positions.items():
            bar = bars.get(code)
            price = bar.raw_close if bar is not None else position.last_price
            self.account.mark_price(code, price)
            close_prices[code] = price
        self.account.assert_consistent(close_prices)
        summary = self._current_target_summary
        snapshot = build_snapshot(
            trading_date=trading_date,
            account=self.account,
            prices=close_prices,
            financing_mode=self.config.financing_mode,
            cost_model=self.cost_model.cost_model,
            trades=self._current_trades,
            daily_interest=self._current_context.daily_interest.accrued,
            pv_target=summary.pv_target_total if summary else None,
            pv_actual=summary.pv_actual_total if summary else None,
        )
        self.previous_close_equity = snapshot.equity
        result = DayResult(
            trading_date=trading_date,
            snapshot=snapshot,
            trades=tuple(self._current_trades),
            liquidation=self._current_liquidation,
            target_summary=summary,
            interest=self._current_context.daily_interest,
            rejection_reason=self._rejection_reason,
        )
        self._current_date = None
        self._current_dataset = None
        self._current_context = None
        return result

    def _capital_base(self) -> Decimal:
        if self.config.capital_base_mode == "initial_equity":
            return self.config.initial_cash
        if self.previous_close_equity is None:
            return self.config.initial_cash
        return max(ZERO, self.previous_close_equity)

    def _validate_bars_and_rules(
        self,
        dataset: MarketDataset,
        trading_date: date,
        target: TargetPortfolio,
    ) -> None:
        errors: list[str] = []
        for code in sorted(target.weights):
            bar = dataset.bar(trading_date, code)
            if bar is None:
                errors.append(f"missing execution bar for {code}")
                continue
            rule, rule_error = dataset.rule_lookup(code, trading_date)
            if rule is None:
                errors.append(f"{rule_error or 'invalid_trading_rule'} for {code}")
            if bar.raw_open <= ZERO:
                errors.append(f"non-positive raw_open for {code}")
        if errors:
            raise PortfolioValidationError(errors)

    def _reject_target(
        self,
        trading_date: date,
        target: TargetPortfolio,
        errors: list[str],
    ) -> None:
        message = "; ".join(errors)
        self._rejection_reason = message
        self.rejected_targets.append(
            {
                "execution_date": trading_date,
                "target": target.to_dict(),
                "errors": errors,
            }
        )
        self.account.record_event(
            event_date=trading_date,
            event_type="invalid_target_portfolio",
            cash_delta=ZERO,
            debt_delta=ZERO,
            position_delta=0,
            details={"errors": errors},
        )

    def _active_sell(
        self,
        dataset: MarketDataset,
        trading_date: date,
        plan: _TradePlan,
        bar: MarketBar,
    ) -> None:
        position = self.account.positions.get(plan.code)
        if position is None or position.size <= plan.target_shares:
            return
        requested = position.size - plan.target_shares
        available = position.available_size
        rule, rule_error = dataset.rule_lookup(plan.code, trading_date)
        if rule_error is not None:
            quote = ExecutionQuote(
                False,
                bar.raw_open,
                ZERO,
                0,
                rule_error,
            )
        else:
            quote = self.execution_model.quote(
                bar, "sell", self.cost_model, rule
            )
        if not quote.tradable:
            self._record_zero_trade(
                plan, trading_date, "sell", quote, requested, quote.reason
            )
            return
        fillable = min(requested, available, quote.adv_cap_shares)
        fillable = (fillable // self.config.lot_size) * self.config.lot_size
        if fillable <= 0:
            reason = "t1_locked" if available == 0 else "no_sell_liquidity"
            self._record_zero_trade(plan, trading_date, "sell", quote, requested, reason)
            return

        gross = money_round(quote.execution_price * decimal_value(fillable))
        fees = self.cost_model.fees("sell", gross, fillable)
        net_proceeds = gross - fees.total
        repay = min(net_proceeds, self.account.debt)
        cash_delta = net_proceeds - repay
        debt_delta = -repay
        slippage = money_round(
            decimal_value(fillable) * abs(quote.execution_price - quote.reference_price)
        )
        self.account.cash_ledger.apply(cash_delta)
        self.account.debt_ledger.apply(debt_delta)
        self.account.apply_position_delta(
            code=plan.code,
            name=bar.name,
            delta=-fillable,
            price=quote.execution_price,
        )
        if fees.minimum_commission_triggered:
            self.account.minimum_fee_order_count += 1
        unfilled = requested - fillable
        if unfilled > 0:
            self.account.unfilled_order_count += 1
        self.account.record_event(
            event_date=trading_date,
            event_type="active_sell",
            cash_delta=cash_delta,
            debt_delta=debt_delta,
            position_delta=-fillable,
            code=plan.code,
            side="sell",
            commission=fees.commission,
            other_fees=fees.other_fees,
            slippage_cost=slippage,
            details={
                "gross_amount": str(gross),
                "net_proceeds": str(net_proceeds),
                "repay": str(repay),
                "reference_price": str(quote.reference_price),
                "execution_price": str(quote.execution_price),
                "unfilled_shares": unfilled,
            },
        )
        plan.filled_sell += fillable
        plan.unfilled += unfilled
        self._current_trades.append(
            TradeExecution(
                signal_date=self._active_target().signal_date,
                execution_date=trading_date,
                code=plan.code,
                name=bar.name,
                side="sell",
                reference_price=quote.reference_price,
                execution_price=quote.execution_price,
                shares=fillable,
                gross_amount=gross,
                commission=fees.commission,
                other_fees=fees.other_fees,
                slippage_cost=slippage,
                net_cash_flow=net_proceeds,
                unfilled_shares=unfilled,
                reason="target_rebalance",
                account_status=self.account.status,
                debt_repaid=repay,
            )
        )

    def _active_buy(
        self,
        dataset: MarketDataset,
        trading_date: date,
        plan: _TradePlan,
        bar: MarketBar,
        credit_limit: Decimal,
    ) -> None:
        position = self.account.positions.get(plan.code)
        current = position.size if position is not None else 0
        if current >= plan.target_shares:
            return
        requested = plan.target_shares - current
        rule, rule_error = dataset.rule_lookup(plan.code, trading_date)
        if rule_error is not None:
            quote = ExecutionQuote(
                False,
                bar.raw_open,
                ZERO,
                0,
                rule_error,
            )
        else:
            quote = self.execution_model.quote(
                bar, "buy", self.cost_model, rule
            )
        if not quote.tradable:
            self._record_zero_trade(
                plan, trading_date, "buy", quote, requested, quote.reason
            )
            return
        affordable = self._affordable_buy_shares(
            quote.execution_price,
            min(requested, quote.adv_cap_shares),
            credit_limit,
        )
        if affordable <= 0:
            self._record_zero_trade(
                plan,
                trading_date,
                "buy",
                quote,
                requested,
                "cash_or_credit_limit",
            )
            return
        requested_before_fill = requested
        fill = affordable
        gross = money_round(quote.execution_price * decimal_value(fill))
        fees = self.cost_model.fees("buy", gross, fill)
        cash_out = gross + fees.total
        paid_from_cash = min(self.account.cash, cash_out)
        borrowed = cash_out - paid_from_cash
        self.account.cash_ledger.apply(-paid_from_cash)
        self.account.debt_ledger.apply(borrowed)
        locked_until = None
        if rule is not None and rule.settlement == "T1":
            # If no later session exists in this fixture, the shares remain
            # locked for the path's lifetime rather than silently becoming T+0.
            locked_until = self._current_context.next_session or date.max
        self.account.apply_position_delta(
            code=plan.code,
            name=bar.name,
            delta=fill,
            price=quote.execution_price,
            locked_until=locked_until,
        )
        if fees.minimum_commission_triggered:
            self.account.minimum_fee_order_count += 1
        slippage = money_round(
            decimal_value(fill) * abs(quote.execution_price - quote.reference_price)
        )
        unfilled = requested_before_fill - fill
        if unfilled > 0:
            self.account.unfilled_order_count += 1
        self.account.record_event(
            event_date=trading_date,
            event_type="active_buy",
            cash_delta=-paid_from_cash,
            debt_delta=borrowed,
            position_delta=fill,
            code=plan.code,
            side="buy",
            commission=fees.commission,
            other_fees=fees.other_fees,
            slippage_cost=slippage,
            details={
                "gross_amount": str(gross),
                "cash_out": str(cash_out),
                "borrowed": str(borrowed),
                "reference_price": str(quote.reference_price),
                "execution_price": str(quote.execution_price),
                "locked_until": locked_until.isoformat() if locked_until else None,
                "unfilled_shares": unfilled,
            },
        )
        plan.filled_buy += fill
        plan.unfilled += unfilled
        target = self._active_target()
        self._current_trades.append(
            TradeExecution(
                signal_date=target.signal_date,
                execution_date=trading_date,
                code=plan.code,
                name=bar.name,
                side="buy",
                reference_price=quote.reference_price,
                execution_price=quote.execution_price,
                shares=fill,
                gross_amount=gross,
                commission=fees.commission,
                other_fees=fees.other_fees,
                slippage_cost=slippage,
                net_cash_flow=-cash_out,
                unfilled_shares=unfilled,
                reason="target_rebalance",
                account_status=self.account.status,
                borrowed_amount=borrowed,
            )
        )

    def _record_zero_trade(
        self,
        plan: _TradePlan,
        trading_date: date,
        side: str,
        quote: ExecutionQuote,
        requested: int,
        reason: str,
    ) -> None:
        self.account.unfilled_order_count += 1
        plan.unfilled += requested
        target = self._active_target()
        self._current_trades.append(
            TradeExecution(
                signal_date=target.signal_date,
                execution_date=trading_date,
                code=plan.code,
                name=plan.name,
                side=side,
                reference_price=quote.reference_price,
                execution_price=ZERO,
                shares=0,
                gross_amount=ZERO,
                commission=ZERO,
                other_fees=ZERO,
                slippage_cost=ZERO,
                net_cash_flow=ZERO,
                unfilled_shares=requested,
                reason=reason,
                account_status=self.account.status,
            )
        )

    def _active_target(self) -> TargetPortfolio:
        target = self._active_target_for_trades
        if target is None:
            raise RuntimeError("No TargetPortfolio is active for trade logging")
        return target

    def _affordable_buy_shares(
        self,
        execution_price: Decimal,
        cap: int,
        credit_limit: Decimal,
    ) -> int:
        if cap < self.config.lot_size:
            return 0
        budget = self.account.cash + max(
            ZERO, credit_limit - self.account.debt
        )
        if self.account.debt > credit_limit:
            return 0
        if budget <= ZERO:
            return 0
        low_lots = 0
        high_lots = cap // self.config.lot_size
        best_lots = 0
        while low_lots <= high_lots:
            mid = (low_lots + high_lots) // 2
            shares = mid * self.config.lot_size
            gross = money_round(execution_price * decimal_value(shares))
            fees = self.cost_model.fees("buy", gross, shares)
            cash_out = gross + fees.total
            if shares > 0 and cash_out <= budget:
                best_lots = mid
                low_lots = mid + 1
            else:
                high_lots = mid - 1
        return best_lots * self.config.lot_size

    def _apply_corporate_actions(
        self, dataset: MarketDataset, trading_date: date
    ) -> None:
        for action in dataset.actions_for(trading_date):
            position = self.account.positions.get(action.code)
            if position is None:
                continue
            if action.action_type == "cash_dividend":
                amount = decimal_value(position.size) * action.cash_per_share
                if amount <= ZERO:
                    continue
                self.account.cash_ledger.apply(amount)
                self.account.record_event(
                    event_date=trading_date,
                    event_type="cash_dividend",
                    cash_delta=amount,
                    debt_delta=ZERO,
                    position_delta=0,
                    code=action.code,
                    details={
                        "effective_date": action.effective_date.isoformat(),
                        "known_at": action.known_at.isoformat(),
                        "cash_per_share": str(action.cash_per_share),
                    },
                )
            elif action.action_type == "split":
                new_size_decimal = decimal_value(position.size) * action.split_ratio
                new_size = int(
                    new_size_decimal.to_integral_value(rounding=ROUND_FLOOR)
                )
                delta = new_size - position.size
                if delta == 0:
                    continue
                locked_ratio = (
                    Decimal(position.locked_size) / Decimal(position.size)
                    if position.size
                    else ZERO
                )
                position.size = new_size
                position.locked_size = min(
                    new_size,
                    int(
                        (Decimal(new_size) * locked_ratio).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                )
                position.average_cost = (
                    position.average_cost / action.split_ratio
                    if action.split_ratio
                    else position.average_cost
                )
                self.account.record_event(
                    event_date=trading_date,
                    event_type="position_split",
                    cash_delta=ZERO,
                    debt_delta=ZERO,
                    position_delta=delta,
                    code=action.code,
                    details={
                        "effective_date": action.effective_date.isoformat(),
                        "known_at": action.known_at.isoformat(),
                        "split_ratio": str(action.split_ratio),
                        "locked_after": position.locked_size,
                    },
                )

    def _liquidation(
        self,
        dataset: MarketDataset,
        trading_date: date,
        bars: dict[str, MarketBar],
        open_prices: dict[str, Decimal],
    ) -> None:
        if self.account.first_liquidation_date is None:
            self.account.first_liquidation_date = trading_date
            self.account.first_liquidation_day_index = self._day_counter
        self.account.status = AccountStatus.FORCED_LIQUIDATION
        line = self.config_liquidation_line()
        margin_open = self.account.margin_ratio(open_prices) or ZERO

        if self.account.cash > ZERO and self.account.debt > ZERO:
            swept = min(self.account.cash, self.account.debt)
            self.account.cash_ledger.apply(-swept)
            self.account.debt_ledger.apply(-swept)
            self.account.record_event(
                event_date=trading_date,
                event_type="liquidation_cash_sweep",
                cash_delta=-swept,
                debt_delta=-swept,
                position_delta=0,
                details={"cash_applied_to_debt": str(swept)},
            )

        debt_before = self.account.debt
        gross = ZERO
        sell_fees = ZERO
        unfilled_total = 0
        details: list[dict[str, Any]] = []
        for code in sorted(self.account.positions):
            position = self.account.positions[code]
            bar = bars.get(code)
            requested = position.size
            if bar is None:
                unfilled_total += requested
                details.append(
                    {
                        "code": code,
                        "name": position.name,
                        "size": requested,
                        "filled_shares": 0,
                        "price": ZERO,
                        "fees": ZERO,
                        "reason": "missing_execution_bar",
                    }
                )
                continue
            rule, rule_error = dataset.rule_lookup(code, trading_date)
            if rule_error is not None:
                quote = ExecutionQuote(
                    False,
                    bar.raw_low,
                    ZERO,
                    0,
                    rule_error,
                )
            else:
                quote = self.execution_model.quote(
                    bar, "sell", self.cost_model, rule, is_liquidation=True
                )
            if not quote.tradable:
                unfilled_total += requested
                self.account.unfilled_order_count += 1
                self._current_trades.append(
                    TradeExecution(
                        signal_date=trading_date,
                        execution_date=trading_date,
                        code=code,
                        name=position.name,
                        side="sell",
                        reference_price=quote.reference_price,
                        execution_price=ZERO,
                        shares=0,
                        gross_amount=ZERO,
                        commission=ZERO,
                        other_fees=ZERO,
                        slippage_cost=ZERO,
                        net_cash_flow=ZERO,
                        unfilled_shares=requested,
                        reason=quote.reason,
                        is_liquidation=True,
                        account_status=self.account.status,
                    )
                )
                details.append(
                    {
                        "code": code,
                        "name": position.name,
                        "size": requested,
                        "filled_shares": 0,
                        "price": ZERO,
                        "fees": ZERO,
                        "reason": quote.reason,
                    }
                )
                continue
            fill = min(requested, quote.adv_cap_shares)
            fill = (fill // self.config.lot_size) * self.config.lot_size
            if fill <= 0:
                unfilled_total += requested
                self.account.unfilled_order_count += 1
                details.append(
                    {
                        "code": code,
                        "name": position.name,
                        "size": requested,
                        "filled_shares": 0,
                        "price": quote.execution_price,
                        "fees": ZERO,
                        "reason": "adv_cap_zero",
                    }
                )
                continue
            trade_gross = money_round(quote.execution_price * decimal_value(fill))
            fees = self.cost_model.fees("sell", trade_gross, fill)
            if fees.minimum_commission_triggered:
                self.account.minimum_fee_order_count += 1
            gross += trade_gross
            sell_fees += fees.total
            slippage = money_round(
                decimal_value(fill)
                * abs(quote.execution_price - quote.reference_price)
            )
            self.account.apply_position_delta(
                code=code,
                name=position.name,
                delta=-fill,
                price=quote.execution_price,
            )
            self.account.record_event(
                event_date=trading_date,
                event_type="liquidation_position",
                cash_delta=ZERO,
                debt_delta=ZERO,
                position_delta=-fill,
                code=code,
                side="sell",
                slippage_cost=slippage,
                details={
                    "gross_amount": str(trade_gross),
                    "fees": str(fees.total),
                    "reference_price": str(quote.reference_price),
                    "execution_price": str(quote.execution_price),
                    "unfilled_shares": requested - fill,
                },
            )
            unfilled = requested - fill
            unfilled_total += unfilled
            if unfilled > 0:
                self.account.unfilled_order_count += 1
            self._current_trades.append(
                TradeExecution(
                    signal_date=trading_date,
                    execution_date=trading_date,
                    code=code,
                    name=position.name,
                    side="sell",
                    reference_price=quote.reference_price,
                    execution_price=quote.execution_price,
                    shares=fill,
                    gross_amount=trade_gross,
                    commission=fees.commission,
                    other_fees=fees.other_fees,
                    slippage_cost=slippage,
                    net_cash_flow=trade_gross - fees.total,
                    unfilled_shares=unfilled,
                    reason="forced_liquidation",
                    is_liquidation=True,
                    account_status=AccountStatus.FORCED_LIQUIDATION,
                )
            )
            details.append(
                {
                    "code": code,
                    "name": position.name,
                    "size": requested,
                    "filled_shares": fill,
                    "price": quote.execution_price,
                    "fees": fees.total,
                    "reason": "forced_liquidation",
                }
            )

        positions_remain = bool(self.account.positions)
        settlement: LiquidationSettlement = settle_liquidation_proceeds(
            debt_before=debt_before,
            gross_proceeds=gross,
            sell_fees=sell_fees,
            positions_remain=positions_remain,
        )
        self.account.cash_ledger.apply(settlement.cash_after)
        self.account.debt_ledger.apply(settlement.debt_after - debt_before)
        self.account.status = settlement.account_status
        if settlement.bad_debt > ZERO:
            self.account.total_bad_debt += settlement.bad_debt
        self.account.record_event(
            event_date=trading_date,
            event_type="liquidation_settlement",
            cash_delta=settlement.cash_after,
            debt_delta=settlement.debt_after - debt_before,
            position_delta=0,
            commission=sum(
                (
                    trade.commission
                    for trade in self._current_trades
                    if trade.is_liquidation
                ),
                ZERO,
            ),
            other_fees=sum(
                (
                    trade.other_fees
                    for trade in self._current_trades
                    if trade.is_liquidation
                ),
                ZERO,
            ),
            details={
                "gross_proceeds": str(gross),
                "sell_fees": str(sell_fees),
                "net_proceeds": str(gross - sell_fees),
                "repay": str(settlement.repay),
                "bad_debt": str(settlement.bad_debt),
                "explicit_bad_debt_writeoff": str(
                    settlement.bad_debt
                ),
                "positions_remain": positions_remain,
            },
        )
        summary = LiquidationSummary(
            trigger_date=trading_date,
            liquidation_line=line,
            margin_ratio_open=margin_open,
            gross_proceeds=gross,
            sell_fees=sell_fees,
            net_proceeds=gross - sell_fees,
            repay=settlement.repay,
            bad_debt=settlement.bad_debt,
            unfilled_shares=unfilled_total,
            positions=details,
            account_status=self.account.status,
        )
        self.liquidations.append(summary)
        self._current_liquidation = summary

