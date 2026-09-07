# SPDX-License-Identifier: Apache-2.0
import uuid
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# The session-secret placeholder vocabulary, defined ONCE (B.13 review f46a31d2,
# finding sample-session-placeholder-passes-config): every value any shipped surface
# uses as a stand-in secret. The startup guard below and the install config-stage
# predicate (install/state.py) both consume THIS set — a placeholder recognized by one
# surface and not the other is exactly the drift that let a copied-unchanged
# .env.example pass validation.
PLACEHOLDER_SESSION_SECRETS = frozenset(
    {
        "dev-insecure-change-me",  # the code default (Settings.session_secret)
        "change-me-to-a-long-random-string",  # the shipped .env.example sample
    }
)


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment / .env (prefix AM_)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AM_", extra="ignore")

    database_url: str = "postgresql+asyncpg://am:am@localhost:5433/assistant_memory"

    # Owner bootstrap (decisions §8): the first/only human who admits others.
    # Keyed on Google sub once OAuth lands (B7); email is the future login pin.
    owner_google_sub: str | None = None
    owner_email: str | None = None

    # The TEST SUITE's own database. Unused by the running app, and it lives here rather
    # than being read from the environment because `.env` is loaded through Settings — an
    # os.environ lookup simply does not see it. tests/conftest.py refuses to run without it:
    # a suite sharing the app's database reads rows it did not create and goes green by luck.
    test_database_url: str | None = None

    # Space routing. Both values are ids, never names: a name is not unique and the
    # name-based lookup is exactly what produced a full duplicate projection of the
    # conventions on 2026-08-10.
    #
    # conventions_space_id -- OPTIONAL pre-provisioning input only (B.13 A-1): on a fresh
    #   install the bootstrap CREATES the conventions home itself and records its id in the
    #   install-settings store in the same transaction — no admin step, no restart. A value
    #   set HERE is adopted once, after the shared existence-and-ownership verification
    #   (install_state.routing_home_problem), and recorded into the store. The store's
    #   recorded id — never this field — is what the consumers of the routing id read:
    #   the bootstrap's seeding, the seed-stage predicate (which demands the projection
    #   IN the recorded home), the repair endpoint, and the publish CLI. The document-
    #   SERVING path is deliberately not routed by it: lookups stay scoped to the
    #   caller's visible spaces.
    # feedback_space_id -- the fixed destination of the maintainer's own `feedback` tool.
    #   The capability is NOT part of the stock install (recorded operator decision F-12):
    #   not provisioned, not configured, not gated — a third-party installation reports
    #   problems through the publication track's channel instead. The caller never chooses
    #   the destination: feedback is about the memory project, wherever the reporter works.
    #
    # UNSET is the ordinary state for both. A value that IS set but does not resolve to an
    # existing space fails startup loudly (bootstrap.validate_configured_spaces).
    conventions_space_id: uuid.UUID | None = None
    feedback_space_id: uuid.UUID | None = None

    # Search / embeddings (B6). Offline multilingual model on the VPS keeps personal
    # data local (plan.md). "deterministic" is a dependency-free dev/test stand-in;
    # "sentence_transformers" loads the real model. dim is locked by the migration.
    embedding_provider: str = "deterministic"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Review-orchestration channel guard: the channel is append-only and consumers replay
    # it whole, so one oversized message bricks a review permanently (review 809987bd).
    # POSTs with a larger payload are refused at the author with a 422. Sized just under
    # the smallest known critic-model input cap (codex: 1048576 chars).
    review_max_message_bytes: int = 1_000_000

    # Web admin / OAuth (B7). The owner registers one Google OAuth client and logs
    # in under that same account (it is pinned as owner via owner_email/sub).
    base_url: str = "http://localhost:8000"
    session_secret: str = "dev-insecure-change-me"
    google_client_id: str | None = None
    google_client_secret: str | None = None

    # Actionable-layer scheduler (docs/design/actionable-layer.md). Optional module (D14):
    # OFF by default — nothing runs until enabled. Slice 1 = a rescan-driven resident runner
    # (LISTEN/NOTIFY precision is a later slice; this rescan is the D6 backstop).
    scheduler_enabled: bool = False
    scheduler_rescan_seconds: int = 30
    scheduler_lease_seconds: int = 300
    # REASON seam (D16): "stub" = deterministic no-op; "openai" = the cheap tier via OpenAI;
    # "strong" = the subscription-CLI strong tier (D17, reuses strong_cli_token/strong_model).
    scheduler_reasoner: str = "stub"
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"  # cheap tier (GPT-5 mini; note: forces temperature=1)

    # Interactive bot (actionable layer, D13). Optional module: OFF by default. Slice A =
    # outbound delivery worker + inbound auth skeleton (real conversation is a later slice).
    bot_enabled: bool = False
    bot_token: str = ""
    bot_operator_chat_ids: str = ""  # CSV of allow-listed Telegram chat ids (D26)
    # chat-id -> effective operator Account (D19/D26), "chat_id:account_uuid,...". Unmapped
    # allow-listed chats fall back to the owner's default account (bot/accounts.py).
    bot_chat_accounts: str = ""
    # Read-only conversation agent (C-2b): OpenAI function-calling over the read tools under the
    # operator's effective account. Explicit opt-in, separate from outbound/elicit; needs an
    # OpenAI key. Write path + §4 untrusted-content framing stay gated by OQ7.
    bot_agent_enabled: bool = False
    # Trust of the agent's principal (TRUSTS: trusted|limited|untrusted). "limited" is the OQ7
    # default: it ingests untrusted content, so writes must not be self-confirmable — "limited"
    # floors every write to confirm + routes to server_proposal (propose-not-act). Not "trusted".
    bot_agent_trust: str = "limited"
    # Agent write surface (W1). OFF → read-only. When ON, the agent may call write tools, but under
    # "limited" trust each write STAGES a proposal (propose-not-act); in-chat approval is W2/W3.
    # No share/unshare ever (D27). Gated: keep OFF until the approve loop + §4 framing land.
    bot_agent_write: bool = False
    # Quiet hours (D28/D29): hold NON-urgent proactive Deliveries during this operator-local window
    # (urgent ones always send). Empty = always send. "22:00-08:00" wraps midnight.
    bot_quiet_hours: str = ""
    bot_timezone: str = "UTC"
    # Strong-tier escalation (slice D, D17): headless Claude Code subprocess on the operator's
    # subscription. OFF by default; needs a token from `claude setup-token` (one-time). Read-only.
    bot_strong_enabled: bool = False
    strong_cli_token: str = ""  # CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`
    strong_model: str = ""  # optional model override (e.g. "opus"); empty = CLI default
    # Where the strong-tier subprocess reaches this app's MCP for READ-ONLY recall (D18). Empty =
    # the strong tier reasons over passed context only (no graph access). E.g. http://127.0.0.1:8010/mcp
    strong_mcp_url: str = ""
    bot_poll_seconds: int = 5

    # Daily Google Drive backup (ops/gdrive_backup, run by the host scheduler via docker exec).
    # Reuses AM_GOOGLE_CLIENT_ID/SECRET + a drive.file refresh token (scripts/gdrive_authorize.py).
    # OFF unless a refresh token is set. The job owns ONE Drive folder (drive.file = its files only)
    # and keeps the N most recent dumps.
    gdrive_refresh_token: str = ""
    gdrive_folder_name: str = "assistant_memory_backups"
    gdrive_keep_last: int = 3

    @model_validator(mode="after")
    def _enforce_write_requires_limited_trust(self) -> "Settings":
        """OQ7 enablement invariant: the agent's write path is safe ONLY under trust='limited',
        which floors every write to confirm and routes it to server_proposal (propose-not-act).
        Under any other trust a normal `allow` write applies SILENTLY (policy.enforcement), breaking
        the write-prompt's central promise. Fail fast at startup rather than trust a stray env var.
        """
        if self.bot_agent_write and self.bot_agent_trust != "limited":
            raise ValueError(
                "AM_BOT_AGENT_WRITE=true requires AM_BOT_AGENT_TRUST='limited' "
                f"(got {self.bot_agent_trust!r}); any other trust lets a normal write apply "
                "silently instead of staging a proposal for approval. Refusing to start."
            )
        return self

    @model_validator(mode="after")
    def _enforce_real_session_secret(self) -> "Settings":
        """session_secret signs the admin session cookie — with the shipped default anyone can
        forge an owner session. Bare local dev is harmless (no OAuth client -> no login path
        mints cookies), so fail fast only when the deployment is real: an OAuth client is
        configured, or base_url points beyond localhost.
        """
        if self.session_secret in PLACEHOLDER_SESSION_SECRETS:
            host = urlsplit(self.base_url).hostname or ""
            if self.google_client_id is not None or host not in _LOCAL_HOSTS:
                raise ValueError(
                    "AM_SESSION_SECRET is still the shipped default while the deployment is "
                    "real (Google OAuth configured and/or non-local AM_BASE_URL) — admin "
                    "session cookies would be forgeable. Generate one: python -c \"import "
                    'secrets; print(secrets.token_urlsafe(48))". Refusing to start.'
                )
        return self


settings = Settings()
