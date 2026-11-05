-- поток: rev. Owner-approved fixed replies apply only to enrolled historical Yandex reviews.
CREATE TABLE IF NOT EXISTS rev_archive_publication (
 platform text NOT NULL, account text NOT NULL, kind text NOT NULL CHECK(kind='review'),
 ext_id text NOT NULL, reply_kind text NOT NULL, approved_text text NOT NULL,
 approval_basis text NOT NULL, state text NOT NULL DEFAULT 'pending', detail text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(platform,account,kind,ext_id)
);
