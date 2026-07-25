-- Shared host lock for coordinating heavy jobs (e.g. subtitle sync ffmpeg)
-- on the same VPS as the Telegram Stremio addon.
--
-- Consumer rule: host is busy only when
--   busy = true AND busy_until > now()
-- No row or expired lease => free to run (crash-safe).

CREATE TABLE IF NOT EXISTS personal_host_busy (
  id text PRIMARY KEY,
  busy boolean NOT NULL DEFAULT false,
  busy_until timestamptz,
  active_streams integer NOT NULL DEFAULT 0,
  source text,
  stream_key text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO personal_host_busy (id, busy, active_streams)
VALUES ('default', false, 0)
ON CONFLICT (id) DO NOTHING;
