"""Errors raised by machinery that more than one strategy package shares."""


class EquityDepletedError(RuntimeError):
    def __init__(
        self,
        *,
        trade_date,
        previous_equity,
        gross_return,
        turnover,
        cost,
        net_return,
        equity,
    ) -> None:
        self.trade_date = trade_date
        self.previous_equity = previous_equity
        self.gross_return = gross_return
        self.turnover = turnover
        self.cost = cost
        self.net_return = net_return
        self.equity = equity
        super().__init__(
            f"{trade_date} equity depleted: previous_equity={previous_equity}, "
            f"gross_return={gross_return}, turnover={turnover}, cost={cost}, "
            f"net_return={net_return}, equity={equity}"
        )
