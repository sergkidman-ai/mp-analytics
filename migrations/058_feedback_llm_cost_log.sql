-- поток: rev
-- Дневной лог расхода LLM на генерацию черновиков (циклов в сутках ~12, _CostTracker in-memory
-- на один процесс недостаточен для суточной сводки).
CREATE TABLE IF NOT EXISTS feedback_llm_cost_log (
    day        date NOT NULL,
    model      text NOT NULL,
    calls      integer NOT NULL DEFAULT 0,
    tokens_in  bigint NOT NULL DEFAULT 0,
    tokens_out bigint NOT NULL DEFAULT 0,
    cost_usd   numeric(12,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);
