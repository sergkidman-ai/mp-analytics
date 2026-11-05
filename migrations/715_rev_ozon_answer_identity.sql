-- поток: rev — the seller name is proven by a known publication and matching API text
CREATE TABLE IF NOT EXISTS rev_ozon_answer_identity(
    account text NOT NULL, author_name text NOT NULL,
    proof_question_id text NOT NULL,proof_answer_id text NOT NULL,
    proof_text_sha256 text NOT NULL,verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(account,author_name)
);
