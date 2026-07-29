-- ============================================================================
-- MEDIA SCHEMA v3 — ADJUDICATED
-- Supersedes ~/tmp/fc-briefings/MEDIA-SCHEMA-V2.md §3-§4 in full.
-- Target: image-manager's own database (media_manager), Postgres 17.
-- Greenfield: no production data worth preserving. DROP AND RECREATE.
--
-- GOVERNING PRINCIPLE (restated, narrowed after adjudication):
--   New EXPRESSIVE bytes are created only when we own the input.
--   A technical re-encode of bytes we are already rehosting is a CACHE, not a
--   derivative; it lives in asset_rendition and is purged with the base.
--
-- WHAT IS STRUCTURAL HERE (not prose):
--   * the render gate is ONE view, asset_derive_state. There is no second copy.
--   * byte facts (watermark / CMI / phash / derivation) live on the bytes and
--     cannot be out-voted by an origin row.
--   * watermark_state is MONOTONIC toward closed. A detector regression cannot
--     re-open a §1202 gate.
--   * derivation depth is clamped to 1 by a composite FK over generated
--     columns; cycles and self-derivation are impossible, not discouraged.
--   * a matte mask cannot exist over a photograph whose derivation was never
--     permitted, and cannot be composited onto an image it was not derived from.
--   * takedown is a VETO (asset_suppression) that ingest can never re-derive.
--   * "Never mutated. Never deleted." is enforced by REVOKE, not by comment.
-- ============================================================================

DROP SCHEMA IF EXISTS media CASCADE;
CREATE SCHEMA media;
SET search_path = media, public;

-- ============================================================================
-- 0. TYPES  (mirrored from the spine 0001_aggregation_base.sql:48,50 — the media
--    DB is a SEPARATE database, so cross-DB type reuse is impossible; the names
--    and orders are kept identical so the two estates read the same.)
-- ============================================================================
CREATE TYPE confidence     AS ENUM ('high','medium','low','unknown');       -- spine :48
CREATE TYPE content_rating AS ENUM ('all_ages','teen','adult','unknown');   -- spine :50
CREATE TYPE visibility     AS ENUM ('private','tenant','public','catalog'); -- v1 models.py:59 CHECK

-- spine house style: CREATE DOMAIN currency AS text CHECK (VALUE ~ ...) at 0001:65.
-- CHAR(64) validated nothing: 'deadbeef', '../../etc/passwd' and an UPPERCASE
-- duplicate of the same content were all accepted as primary keys.
CREATE DOMAIN sha256_hex AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- 'unknown' must be the MOST restrictive for age gating, which the enum's own
-- ordinal position is not. Explicit rank; used by the album share threshold.
CREATE FUNCTION rating_rank(content_rating) RETURNS int
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT CASE $1 WHEN 'all_ages' THEN 0 WHEN 'teen' THEN 1 WHEN 'adult' THEN 2 ELSE 3 END $$;

-- Full sha256, never md5: source_url is scraped and attacker-influenced, and a
-- collision here would silently merge two origins (spine 0001:147-152 reasoning).
-- convert_to() is STABLE only because a target encoding could in principle change;
-- pinned to UTF8 it is deterministic, so the wrapper is IMMUTABLE. Do NOT replace
-- this with source_url::bytea -- TESTED: byteain parses backslash escapes and
-- raises "invalid input syntax for type bytea" on any URL containing a backslash,
-- which would abort the ingest transaction.
CREATE FUNCTION url_hash(text) RETURNS bytea
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT CASE WHEN $1 IS NULL THEN NULL ELSE sha256(convert_to($1,'UTF8')) END $$;

-- ============================================================================
-- 1. asset — immutable bytes. NO rights opinion beyond what the BYTES carry.
-- ============================================================================
CREATE TABLE asset (
  sha256        sha256_hex PRIMARY KEY,
  mime          text   NOT NULL,
  width         integer, height integer, bytes bigint,

  -- BYTE FACTS. One sha256 = one pixel buffer = one truth. These were on
  -- asset_origin in v2, where two origins of the same bytes contradicted each
  -- other and the more permissive one opened the §1202 gate.
  watermark_state  text NOT NULL DEFAULT 'unchecked'
    CHECK (watermark_state IN ('clean','watermarked','unchecked')),
  cmi_present      boolean,                 -- NULL = unchecked
  cmi              jsonb,                   -- WHAT the IPTC/XMP rights fields said.
                                            -- §1202 requires knowing the notice, not
                                            -- merely that one existed.
  detector_version text,
  phash            text,
  content_rating   content_rating NOT NULL DEFAULT 'unknown',   -- v1 ImageVersion.age_rating:60

  -- DERIVATION. Single pointer, depth clamped to 1 by parent_must_be_permitted_root.
  derived_from        sha256_hex,
  derived_under_basis text,          -- the authority in force at the moment we derived
  derive_ok_latched   boolean NOT NULL DEFAULT false,  -- monotonic; "may be a parent"

  -- content address IS the location. v2's free-form storage_key is what made the
  -- tombstone hack (UPDATE asset SET storage_key='TOMBSTONE/...') work on a table
  -- documented "never mutated".
  storage_key   text GENERATED ALWAYS AS
                  ('sha256/'||substr(sha256,1,2)||'/'||substr(sha256,3,2)||'/'||sha256) STORED,

  derive_depth  smallint GENERATED ALWAYS AS (CASE WHEN derived_from IS NULL THEN 0 ELSE 1 END) STORED,
  parent_depth  smallint GENERATED ALWAYS AS (CASE WHEN derived_from IS NULL THEN NULL ELSE 0 END) STORED,
  parent_latch  boolean  GENERATED ALWAYS AS (CASE WHEN derived_from IS NULL THEN NULL ELSE true END) STORED,
  is_derivation boolean  GENERATED ALWAYS AS (derived_from IS NOT NULL) STORED,

  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT asset_no_self_derive CHECK (derived_from IS DISTINCT FROM sha256),
  CONSTRAINT asset_basis_iff_derived CHECK ((derived_from IS NULL) = (derived_under_basis IS NULL))
);

-- FK targets. asset_derivation_key also makes "parent must be a PERMITTED root"
-- expressible as one FK: MATCH SIMPLE means the whole constraint is skipped when
-- derived_from IS NULL (parent_depth/parent_latch are then NULL).
CREATE UNIQUE INDEX asset_derivation_key ON asset (sha256, derive_depth, derive_ok_latched);
CREATE UNIQUE INDEX asset_is_derivation_key ON asset (sha256, is_derivation);
CREATE UNIQUE INDEX asset_parentage_key ON asset (sha256, derived_from);
ALTER TABLE asset ADD CONSTRAINT parent_must_be_permitted_root
  FOREIGN KEY (derived_from, parent_depth, parent_latch)
  REFERENCES asset (sha256, derive_depth, derive_ok_latched);
CREATE INDEX asset_derived_from ON asset (derived_from) WHERE derived_from IS NOT NULL;
CREATE INDEX asset_phash ON asset (phash) WHERE phash IS NOT NULL;

-- watermark_state MONOTONIC toward closed. Nothing in v2 or in the reviews
-- stopped a re-run of an older detector flipping 'watermarked' back to 'clean'
-- and silently re-opening the gate. Downgrade requires a superuser/DBA.
CREATE FUNCTION watermark_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.watermark_state = 'watermarked' AND NEW.watermark_state <> 'watermarked' THEN
    RAISE EXCEPTION 'watermark_state is monotonic toward closed (% -> %) on %',
      OLD.watermark_state, NEW.watermark_state, NEW.sha256;
  END IF;
  IF OLD.derive_ok_latched AND NOT NEW.derive_ok_latched THEN
    RAISE EXCEPTION 'derive_ok_latched is monotonic; revoke via asset_suppression';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER asset_monotonic BEFORE UPDATE ON asset
  FOR EACH ROW EXECUTE FUNCTION watermark_monotonic();

-- ============================================================================
-- 2. source_policy — LOCAL replica of the spine's per-source image policy.
--    v2 §4 ended with "AND source.image_derive_ok"; source lives in the SPINE
--    database (0001:70). A fail-closed control cannot contain a network call.
--    Refreshed by a job; STALE => CLOSED.
-- ============================================================================
CREATE TABLE source_policy (
  source_id        uuid PRIMARY KEY,     -- soft ref to spine source.id
  site             text NOT NULL,
  image_hotlink_ok boolean NOT NULL DEFAULT false,
  image_rehost_ok  boolean NOT NULL DEFAULT false,
  image_derive_ok  boolean NOT NULL DEFAULT false,
  takedown_contact text,
  refreshed_at     timestamptz NOT NULL DEFAULT now()
);

-- spine merge-as-redirect (0001:217, :488). Resolved at READ with COALESCE.
-- Never rewrite depiction.product_id: that destroys un-merge reversibility.
CREATE TABLE product_redirect (
  loser_product_id  uuid PRIMARY KEY,
  winner_product_id uuid NOT NULL,
  merged_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (loser_product_id <> winner_product_id)
);

-- ============================================================================
-- 3. asset_origin — how we came to hold these bytes, and what we may do.
--    Acquisition facts ONLY. Byte facts moved to asset.
-- ============================================================================
CREATE TABLE asset_origin (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_sha256     sha256_hex NOT NULL REFERENCES asset(sha256),
  source_id        uuid REFERENCES source_policy(source_id),  -- NULL = first-party
  source_url       text,
  capture_id       uuid,     -- SOFT reference to raw.capture, which is NOT BUILT
                             -- (grep of fc-aggregation: zero hits). Deliberately
                             -- not an FK; add the FK in the migration that creates
                             -- raw.capture. Kept because it cannot be backfilled
                             -- once the captures are gone.
  fetched_at       timestamptz NOT NULL,
  source_class     text NOT NULL,
  rights_basis     text NOT NULL,
  derive_permitted boolean NOT NULL DEFAULT false,     -- THE GATE. Default DENY.

  -- the permission's own provenance. v2 recorded a boolean with no answer to
  -- "who granted this, when, under what instrument" — the missing half of a
  -- control whose purpose is making good faith demonstrable.
  permission_ref    text,
  rights_asserted_by text,
  asserted_at       timestamptz,

  ingested_at      timestamptz NOT NULL DEFAULT now(),

  source_url_hash  bytea GENERATED ALWAYS AS (url_hash(source_url)) STORED,
  -- 'derived_own' origins may only attach to an asset that HAS a parent.
  -- MATCH SIMPLE: NULL for every other class => constraint skipped.
  requires_parent  boolean GENERATED ALWAYS AS
    (CASE WHEN source_class = 'derived_own' THEN true ELSE NULL END) STORED,

  CONSTRAINT origin_source_class CHECK (source_class IN
    ('manufacturer_press','retailer_studio','user_photo','derived_own','unknown')),
  CONSTRAINT origin_rights_basis CHECK (rights_basis IN
    ('user_licence','permission_granted','unlicensed_norm','own_work','unknown')),
  -- v2 accepted ('unlicensed_norm', derive_permitted=TRUE). The gate's premise is
  -- that permission is AFFIRMATIVE.
  CONSTRAINT origin_permission_needs_basis CHECK
    (NOT derive_permitted OR rights_basis IN ('user_licence','permission_granted','own_work')),
  CONSTRAINT origin_grant_names_instrument CHECK
    (rights_basis <> 'permission_granted' OR nullif(btrim(permission_ref),'') IS NOT NULL),
  -- our own derivation is by definition first-party; a 'derived_own' origin
  -- carrying a source_id would mean we scraped our own mask, which is not a
  -- derivation record.
  CONSTRAINT origin_derived_own_is_first_party CHECK
    (source_class <> 'derived_own' OR (source_id IS NULL AND rights_basis = 'own_work')),
  CONSTRAINT parent_asset_must_exist
    FOREIGN KEY (asset_sha256, requires_parent) REFERENCES asset (sha256, is_derivation)
);

-- NULLS NOT DISTINCT. v2 omitted it; source_id is NULL for every user upload, so
-- three byte-identical uploads produced three origin rows carrying potentially
-- contradictory derive_permitted. The spine wrote this lesson down twice
-- (0001:176-179 "verified live: NULL defeated the index", and 0001:459).
-- Hashing source_url keeps the index at 1/6 the size of indexing full URLs;
-- FULL sha256, never md5 — source_url is scraped and attacker-influenced.
CREATE UNIQUE INDEX asset_origin_dedup ON asset_origin
  (asset_sha256, source_id, source_url_hash) NULLS NOT DISTINCT;
-- the takedown screen's access path. v2 left the one query that must be fast
-- under legal pressure as a sequential scan.
CREATE INDEX asset_origin_source ON asset_origin (source_id) INCLUDE (asset_sha256);
CREATE INDEX asset_origin_capture ON asset_origin (capture_id) WHERE capture_id IS NOT NULL;

-- ============================================================================
-- 4. asset_suppression — the takedown VETO. Not an origin. Never re-derived.
--    v2 had no takedown path at all: DELETE was blocked by FKs, storage_key was
--    NOT NULL, disabling every presentation still left the raw bytes serving,
--    and revoking one source's origin could not close a gate another origin
--    held open. Worse, an unrelated later ingest RE-OPENED a closed gate.
-- ============================================================================
CREATE TABLE asset_suppression (
  asset_sha256   sha256_hex PRIMARY KEY REFERENCES asset(sha256),
  reason         text NOT NULL,
  notice_ref     text,
  suppressed_at  timestamptz NOT NULL DEFAULT now(),
  suppressed_by  text NOT NULL,
  lifted_at      timestamptz,
  lifted_by      text,
  lift_reason    text,
  CONSTRAINT lift_is_dated CHECK ((lifted_at IS NULL) = (lifted_by IS NULL)),
  CONSTRAINT lift_after_suppress CHECK (lifted_at IS NULL OR lifted_at >= suppressed_at)
);
CREATE INDEX asset_suppression_live ON asset_suppression (asset_sha256) WHERE lifted_at IS NULL;

-- ============================================================================
-- 5. depiction — a sourced CLAIM that an asset shows a product.
--    IDENTITY: (source, SUBJECT, asset, role).
--      * C2's key put product_id in it: a re-crawl AFTER ER assigns product_id
--        produces a SECOND row (TESTED). The spine excludes product_id from
--        claim_dedup (0001:179) for exactly this reason.
--      * the defence's key left the subject out entirely: one shelf photo
--        showing three figures collapsed to ONE row, and a retailer comparison
--        shot naming two variants collapsed to one (TESTED). Silent data loss
--        on the two roles the enum exists to serve.
--      * subject_key is the SOURCE's stable pre-ER handle: its own item id, or,
--        when the claim is about a product rather than about a listing, the
--        product it names. Neither moves when ER runs.
-- ============================================================================
CREATE TABLE depiction (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_sha256  sha256_hex NOT NULL REFERENCES asset(sha256),
  source_id     uuid REFERENCES source_policy(source_id),  -- NULL = first-party
  source_native_id text,   -- the source's own id for the LISTED ITEM (pre-ER, stable)
  subject_product_id uuid, -- set when the claim's subject is a PRODUCT, not the listing
                           -- (user shelf photos; multi-variant comparison shots)
  product_id    uuid,      -- ER-assigned, MUTABLE, nullable. spine 0001:137.
                           -- v2's NOT NULL blocked media ingest until ER resolved.
  role          text NOT NULL,
  alt_text      text,      -- spine 0001:457 "required on uploads/imports (accessibility)"
  source_url    text,      -- the LISTING page that asserted it, not the CDN byte URL
  ruleset_version text,    -- purge-by-ruleset when an extractor attaches wrong photos
  conf          confidence NOT NULL DEFAULT 'unknown',   -- v2's TEXT sorted high<low<medium
  rank          integer,   -- SOURCE TIER (spine source_attribute_tier.attr_rank:99,
                           -- "lower = more authoritative"). Deliberately NOT unique:
                           -- two equally authoritative sources are normal.
  as_of         timestamptz NOT NULL,      -- FIRST ingested. Immutable.
  last_seen_at  timestamptz NOT NULL,      -- recency. spine 0001:157-168.
  ingested_at   timestamptz NOT NULL DEFAULT now(),

  subject_key   text GENERATED ALWAYS AS
    (COALESCE(source_native_id,'') || '|' || COALESCE(subject_product_id::text,'')) STORED,

  CONSTRAINT depiction_role CHECK (role IN
    ('main','box','detail','scale_ref','user_shelf','comparison')),
  CONSTRAINT depiction_recency_ordered CHECK (last_seen_at >= as_of),   -- spine 0001:174
  -- a claim with no stable subject handle cannot be deduped; refuse it LOUDLY
  -- rather than silently merging every such claim from that source into one row.
  CONSTRAINT depiction_has_subject
    CHECK (source_native_id IS NOT NULL OR subject_product_id IS NOT NULL),
  CONSTRAINT depiction_first_party_names_product
    CHECK (source_id IS NOT NULL OR subject_product_id IS NOT NULL)
);
CREATE UNIQUE INDEX depiction_dedup ON depiction
  (source_id, subject_key, asset_sha256, role) NULLS NOT DISTINCT;
CREATE INDEX depiction_product ON depiction (product_id, role, rank);
CREATE INDEX depiction_asset   ON depiction (asset_sha256);
CREATE INDEX depiction_ruleset ON depiction (ruleset_version) WHERE ruleset_version IS NOT NULL;

-- spine 0001:181-185: a column default cannot reference a sibling column.
CREATE FUNCTION default_last_seen_at() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN IF NEW.last_seen_at IS NULL THEN NEW.last_seen_at := NEW.as_of; END IF; RETURN NEW; END $$;
CREATE TRIGGER depiction_last_seen_default BEFORE INSERT ON depiction
  FOR EACH ROW EXECUTE FUNCTION default_last_seen_at();

-- ============================================================================
-- 6. presentation — how we render, as DATA. Base bytes never touched.
-- ============================================================================
CREATE TABLE presentation (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  base_asset_sha256  sha256_hex NOT NULL REFERENCES asset(sha256),
  render_context     text NOT NULL DEFAULT 'default',  -- one asset legitimately carries
                                                       -- a CaseShelf rotation AND a
                                                       -- detail-page transform
  layer_type         text NOT NULL,
  layer_asset_sha256 sha256_hex REFERENCES asset(sha256),
  z_index            smallint NOT NULL DEFAULT 0,
  transform          jsonb NOT NULL DEFAULT '{}',
  composite_op       text NOT NULL DEFAULT 'source-over',
  produced_by        text NOT NULL,
  enabled            boolean NOT NULL DEFAULT true,
  disabled_at        timestamptz,
  disabled_reason    text,
  created_at         timestamptz NOT NULL DEFAULT now(),

  -- a matte mask may only be composited onto the image it was DERIVED FROM.
  mask_base   sha256_hex GENERATED ALWAYS AS
    (CASE WHEN layer_type = 'matte_mask' THEN base_asset_sha256 ELSE NULL END) STORED,

  CONSTRAINT pres_layer_type CHECK (layer_type IN
    ('matte_mask','occluder','depth_transform','watermark')),
  CONSTRAINT pres_render_context CHECK (render_context IN ('default','case_shelf','detail')),
  -- v2's one-directional CHECK accepted enabled=TRUE with disabled_at and
  -- disabled_reason='DMCA takedown' still set: a failed takedown that looks
  -- like a completed one in every audit query.
  CONSTRAINT pres_enabled_iff_no_disable CHECK (enabled = (disabled_at IS NULL)),
  CONSTRAINT pres_disable_needs_reason   CHECK
    (disabled_at IS NULL OR nullif(btrim(disabled_reason),'') IS NOT NULL),
  CONSTRAINT pres_disable_after_create   CHECK (disabled_at IS NULL OR disabled_at >= created_at),
  -- unconstrained free text flowing into a client-side canvas operation
  CONSTRAINT pres_composite_op CHECK (composite_op IN
    ('source-over','destination-in','destination-out','multiply','screen')),
  CONSTRAINT pres_layer_asset_iff_not_transform CHECK
    ((layer_type = 'depth_transform') = (layer_asset_sha256 IS NULL)),
  CONSTRAINT pres_no_self_composite CHECK (layer_asset_sha256 IS DISTINCT FROM base_asset_sha256),
  CONSTRAINT pres_mask_derived_from_base
    FOREIGN KEY (layer_asset_sha256, mask_base) REFERENCES asset (sha256, derived_from)
);
-- one live layer of a kind per (base, context). C3's key omitted render_context
-- and rejected a legitimate second depth_transform; the defence's includes it.
CREATE UNIQUE INDEX presentation_one_live ON presentation
  (base_asset_sha256, render_context, layer_type) WHERE enabled;
-- deterministic composite order: 3 enabled layers at the same z_index made the
-- render non-reproducible, and an occluder sorting above a watermark obscures CMI.
CREATE UNIQUE INDEX presentation_z ON presentation
  (base_asset_sha256, render_context, z_index) WHERE enabled;
CREATE INDEX presentation_base ON presentation
  (base_asset_sha256, render_context, z_index) WHERE enabled;

-- two mutable columns cannot record a history, and "good faith demonstrable"
-- IS a history. Append-only.
CREATE TABLE presentation_event (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  presentation_id uuid NOT NULL REFERENCES presentation(id),
  event           text NOT NULL CHECK (event IN ('created','disabled','re_enabled')),
  reason          text,
  actor           text NOT NULL,
  at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pres_event_disable_has_reason CHECK
    (event <> 'disabled' OR nullif(btrim(reason),'') IS NOT NULL)
);
CREATE INDEX presentation_event_pres ON presentation_event (presentation_id, at);

-- ============================================================================
-- 7. presentation_job — in-flight work. v2 had no vocabulary for it at all:
--    an OOMed BiRefNet run was indistinguishable from "never queued", and a
--    retry minted new mask bytes => a new sha256 => a duplicate live layer.
-- ============================================================================
CREATE TABLE presentation_job (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  base_asset_sha256 sha256_hex NOT NULL REFERENCES asset(sha256),
  render_context    text NOT NULL DEFAULT 'default',
  layer_type        text NOT NULL,
  produced_by       text NOT NULL,
  state             text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued','running','succeeded','failed','skipped_rights')),
  attempts          integer NOT NULL DEFAULT 0,
  max_attempts      integer NOT NULL DEFAULT 3,
  last_error        text,
  staged_key        text,   -- the S3 object written BEFORE the txn -> orphan reaper
  queued_at         timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  CONSTRAINT job_terminal_is_dated CHECK
    ((state IN ('succeeded','failed','skipped_rights')) = (finished_at IS NOT NULL)),
  CONSTRAINT job_failure_has_reason CHECK
    (state <> 'failed' OR nullif(btrim(last_error),'') IS NOT NULL)
);
CREATE UNIQUE INDEX presentation_job_one_live ON presentation_job
  (base_asset_sha256, render_context, layer_type) WHERE state IN ('queued','running');
CREATE INDEX presentation_job_queue ON presentation_job (state, queued_at);

-- ============================================================================
-- 8. asset_rendition — TECHNICAL variants (resize / transcode). A CACHE.
--    v2 §3.6 forbade new bytes for images we don't own, which meant every
--    scraped press shot served at full resolution to every phone. Egress is the
--    binding cost under BUY-NOTHING. A resize is the SAME expression at a
--    different delivery size; given that we are already rehosting the full file,
--    its marginal exposure is ~0. Matting/cropping is NOT in here — that is the
--    Brammer question and it lives in presentation.
--    NOTE: ON DELETE CASCADE would be dead code (asset is REVOKE-DELETE, and its
--    own derivation FK blocks the DELETE anyway — TESTED). Purge is a TRIGGER on
--    suppression plus a serve-time veto.
-- ============================================================================
CREATE TABLE asset_rendition (
  base_asset_sha256 sha256_hex NOT NULL REFERENCES asset(sha256),
  transform_hash    text NOT NULL,
  transform         jsonb NOT NULL,
  mime              text NOT NULL,
  width integer, height integer, bytes bigint,
  storage_key       text NOT NULL UNIQUE,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (base_asset_sha256, transform_hash),
  -- a rendition is a re-encode, never an expressive edit. Crop/matte/rotate
  -- belong to presentation; if one appears here the cache has become a
  -- derivative-work factory.
  CONSTRAINT rendition_is_technical CHECK
    (NOT (transform ?| ARRAY['crop','matte','mask','rotate','skew','remove_bg','watermark']))
);

CREATE TABLE blob_purge_queue (
  storage_key text PRIMARY KEY,
  reason      text NOT NULL,
  queued_at   timestamptz NOT NULL DEFAULT now(),
  purged_at   timestamptz
);

CREATE FUNCTION purge_renditions_on_suppression() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO blob_purge_queue (storage_key, reason)
    SELECT r.storage_key, 'asset_suppression:'||NEW.asset_sha256
      FROM asset_rendition r WHERE r.base_asset_sha256 = NEW.asset_sha256
    ON CONFLICT (storage_key) DO NOTHING;
  DELETE FROM asset_rendition WHERE base_asset_sha256 = NEW.asset_sha256;
  RETURN NEW;
END $$;
CREATE TRIGGER asset_suppression_purges_renditions AFTER INSERT ON asset_suppression
  FOR EACH ROW EXECUTE FUNCTION purge_renditions_on_suppression();

-- ============================================================================
-- 9. RETAINED FROM v1, rekeyed to asset.sha256.
--    Every `version_id` column is DROPPED: ImageVersion no longer exists and
--    four tables carried a dangling pointer to it (models.py:79,126,167,92).
--    visibility and age_rating did NOT "move to the serve layer" in v2 — no
--    column was added anywhere, while Album.share_age_threshold (models.py:112)
--    and AlbumItem.item_visibility (:127) were retained to compare against them.
-- ============================================================================
CREATE TABLE user_asset_link (                      -- v1 UserImageLink
  user_id      uuid NOT NULL,
  tenant_id    uuid,
  asset_sha256 sha256_hex NOT NULL REFERENCES asset(sha256),
  role         text,
  visibility   visibility NOT NULL DEFAULT 'private',
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, asset_sha256)
);
CREATE INDEX user_asset_visibility ON user_asset_link (user_id, visibility);

CREATE TABLE album (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid,
  owner_user_id       uuid,
  title               text NOT NULL,
  description         text,
  default_visibility  visibility NOT NULL DEFAULT 'private',
  is_shareable        boolean NOT NULL DEFAULT false,
  allow_item_override boolean NOT NULL DEFAULT true,
  share_token_hash    text,
  share_age_threshold content_rating NOT NULL DEFAULT 'all_ages',  -- now comparable again
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  CONSTRAINT album_visibility_not_catalog CHECK (default_visibility <> 'catalog')
);

-- v1's PK was (album_id, position): reordering an album was a primary-key
-- update cascade. Free to fix at drop-and-recreate.
CREATE TABLE album_item (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  album_id        uuid NOT NULL REFERENCES album(id) ON DELETE CASCADE,
  position        integer NOT NULL,
  asset_sha256    sha256_hex NOT NULL REFERENCES asset(sha256),
  item_visibility visibility,
  UNIQUE (album_id, position) DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX album_item_album ON album_item (album_id, position);

CREATE TABLE tag (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  scope         text NOT NULL CHECK (scope IN ('global','tenant','user')),
  tenant_id     uuid,
  owner_user_id uuid
);
CREATE UNIQUE INDEX tag_name_scope ON tag (lower(name), scope, tenant_id, owner_user_id) NULLS NOT DISTINCT;

CREATE TABLE asset_tag (
  asset_sha256  sha256_hex NOT NULL REFERENCES asset(sha256),
  tag_id        uuid NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  tenant_id     uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
  owner_user_id uuid,
  PRIMARY KEY (asset_sha256, tag_id, tenant_id)
);

CREATE TABLE album_tag (
  album_id uuid NOT NULL REFERENCES album(id) ON DELETE CASCADE,
  tag_id   uuid NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  PRIMARY KEY (album_id, tag_id)
);

CREATE TABLE external_ref (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ref_type     text NOT NULL,
  ref_id       text NOT NULL,
  asset_sha256 sha256_hex NOT NULL REFERENCES asset(sha256),
  tenant_id    uuid,
  UNIQUE (ref_type, ref_id)
);

CREATE TABLE service_client (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  secret_hash text NOT NULL,
  scopes      text NOT NULL
);

CREATE TABLE audit_event (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id  uuid,
  actor_service  text,
  tenant_id      uuid,
  asset_sha256   sha256_hex,
  action         text NOT NULL,
  details        jsonb NOT NULL DEFAULT '{}',
  ts             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_event_asset ON audit_event (asset_sha256, ts);

-- ============================================================================
-- 10. THE RENDER GATE — ONE definition, one text to audit.
--     Scored 13/13 against the adjudication fixture set. v2 §4 scored 9/13,
--     C1's unanimity 9/13, the defence's precedence 11/13.
-- ============================================================================
CREATE VIEW asset_derive_state AS
SELECT a.sha256,

  -- PERMISSION, in precedence order:
  -- (1) a DOCUMENTED third-party grant wins outright. An absence-of-grant row
  --     (HLJ redistributing the same press shot) does NOT veto a real licence.
  --     Blanket unanimity gets the many-origins case backwards, which is the
  --     case §3.2 exists to serve.
  -- (2) a FIRST-PARTY claim wins ONLY if these bytes carry no press/retailer
  --     origin. Content dedup makes "Ross's photo" and "the AmiAmi press shot"
  --     literally the same row; a user cannot self-certify bytes we downloaded
  --     from a retailer first. This kills laundering with or without a watermark.
  -- (3) otherwise closed.
  ( EXISTS (SELECT 1 FROM asset_origin o
              JOIN source_policy sp ON sp.source_id = o.source_id
             WHERE o.asset_sha256 = a.sha256
               AND o.derive_permitted
               AND o.rights_basis IN ('permission_granted','own_work')
               AND sp.image_derive_ok
               AND sp.refreshed_at > now() - interval '7 days')   -- stale policy => closed
    OR ( EXISTS (SELECT 1 FROM asset_origin o
                  WHERE o.asset_sha256 = a.sha256 AND o.derive_permitted
                    AND o.rights_basis IN ('user_licence','own_work')
                    AND o.source_id IS NULL)
         AND NOT EXISTS (SELECT 1 FROM asset_origin o
                          WHERE o.asset_sha256 = a.sha256
                            AND o.source_class IN ('manufacturer_press','retailer_studio')) )
  ) AS permission_ok,

  -- BYTE FACTS. Unvotable, because they live on the bytes.
  -- Watermark: unchecked == watermarked. No carve-out — a watermark is visible
  -- pixels and we do not assume bytes are ours.
  -- CMI: §1202 is about removing ANOTHER's copyright management information.
  -- A first-party photograph carrying the photographer's OWN EXIF copyright must
  -- not be blocked by it — that closed the gate on the primary use case, silently.
  ( a.watermark_state = 'clean'
    AND ( COALESCE(a.cmi_present, true) = false
          OR NOT EXISTS (SELECT 1 FROM asset_origin o
                          WHERE o.asset_sha256 = a.sha256 AND o.source_id IS NOT NULL) )
  ) AS bytes_ok,

  NOT EXISTS (SELECT 1 FROM asset_suppression s
               WHERE s.asset_sha256 = a.sha256 AND s.lifted_at IS NULL) AS not_suppressed
FROM asset a;

CREATE VIEW asset_derive_ok AS
SELECT sha256, (permission_ok AND bytes_ok AND not_suppressed) AS derive_ok
FROM asset_derive_state;

-- what the serve path reads. A suppressed ANCESTOR takes its descendants down.
CREATE VIEW renderable_presentation AS
SELECT p.*
FROM presentation p
JOIN asset_derive_state ds ON ds.sha256 = p.base_asset_sha256
JOIN asset a               ON a.sha256  = p.base_asset_sha256
LEFT JOIN asset_suppression ps
       ON ps.asset_sha256 = a.derived_from AND ps.lifted_at IS NULL
WHERE p.enabled
  AND ds.permission_ok AND ds.bytes_ok AND ds.not_suppressed
  AND ps.asset_sha256 IS NULL;

-- renditions are a cache of bytes we rehost; suppression vetoes them too.
CREATE VIEW renderable_rendition AS
SELECT r.* FROM asset_rendition r
WHERE NOT EXISTS (SELECT 1 FROM asset_suppression s
                   WHERE s.asset_sha256 = r.base_asset_sha256 AND s.lifted_at IS NULL);

-- v2 permitted assets with ZERO origins: bytes on disk with no rights record.
-- The gate fails closed for them, so the risk is audit INVISIBILITY, not
-- unlawful serving. A deferred constraint trigger on every insert is
-- over-engineering at this scale; this makes them findable in one query.
CREATE VIEW asset_without_origin AS
SELECT a.sha256, a.created_at FROM asset a
WHERE NOT EXISTS (SELECT 1 FROM asset_origin o WHERE o.asset_sha256 = a.sha256);

-- the latch is the FK-enforceable "may be a derivation parent". It must never be
-- settable ahead of the gate, or an app bug re-opens derivation by UPDATE.
CREATE FUNCTION latch_requires_gate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.derive_ok_latched AND NOT COALESCE(OLD.derive_ok_latched,false)
     AND NOT (SELECT derive_ok FROM asset_derive_ok WHERE sha256 = NEW.sha256) THEN
    RAISE EXCEPTION 'derive_ok_latched refused: gate is closed for %', NEW.sha256;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER asset_latch_guard BEFORE UPDATE ON asset
  FOR EACH ROW EXECUTE FUNCTION latch_requires_gate();

-- ============================================================================
-- 11. GRANTS — "Never mutated. Never deleted." made structural, not asserted.
-- ============================================================================
-- DEVIATION FROM THE ADJUDICATED FILE (the only one; see 0002_media_schema_v3.py).
-- The adjudicated script is a from-scratch bootstrap and opens with
--   DROP ROLE IF EXISTS media_app; CREATE ROLE media_app NOLOGIN;
-- A ROLE is a CLUSTER-wide object, not a schema-scoped one, so DROP ROLE from
-- inside a migration (a) fails outright when the role owns objects in any
-- other database in the cluster, and (b) would revoke a role another database
-- is relying on.  Created idempotently instead; the grants below are
-- unchanged and are what actually make "never mutated, never deleted"
-- structural.
DO $role$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'media_app') THEN
    CREATE ROLE media_app NOLOGIN;
  END IF;
END
$role$;
GRANT USAGE ON SCHEMA media TO media_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA media TO media_app;

REVOKE UPDATE, DELETE ON asset FROM media_app;              -- bytes: insert-only
GRANT  UPDATE (watermark_state, cmi_present, cmi, detector_version, phash,
               content_rating, derive_ok_latched) ON asset TO media_app;

REVOKE UPDATE, DELETE ON depiction FROM media_app;          -- claims: immutable VALUES
GRANT  UPDATE (product_id, last_seen_at, rank, conf) ON depiction TO media_app;  -- spine 0001:132-134

REVOKE UPDATE, DELETE ON asset_origin FROM media_app;       -- provenance: append-only
GRANT  UPDATE (derive_permitted, permission_ref, rights_asserted_by, asserted_at)
       ON asset_origin TO media_app;

REVOKE DELETE ON presentation FROM media_app;               -- kill switch, never erase
REVOKE UPDATE, DELETE ON presentation_event, audit_event FROM media_app;
REVOKE DELETE ON asset_suppression FROM media_app;          -- lift is an UPDATE, not a DELETE
