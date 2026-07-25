-- Run once in Supabase SQL editor (media_mappings).
-- Adds TorBox-backed streams alongside existing Telegram message mappings.

ALTER TABLE media_mappings
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'telegram',
  ADD COLUMN IF NOT EXISTS torbox_kind text,
  ADD COLUMN IF NOT EXISTS torbox_id bigint,
  ADD COLUMN IF NOT EXISTS torbox_file_id integer,
  ADD COLUMN IF NOT EXISTS torbox_hash text,
  ADD COLUMN IF NOT EXISTS torbox_status text;

CREATE INDEX IF NOT EXISTS media_mappings_torbox_lookup_idx
  ON media_mappings (source, torbox_kind, torbox_id, torbox_file_id)
  WHERE source = 'torbox';
