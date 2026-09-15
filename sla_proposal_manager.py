#!/usr/bin/env python3
"""
Account SLA & Proposal Manager
================================

WHAT THIS IS, IN PLAIN ENGLISH
-------------------------------
Every consultancy or F&B/events operation with named enterprise accounts
(a McKinsey, a Gulf Energy, an embassy) is really running on one asset that
never shows up on a balance sheet: the account manager's memory. Which
account is touchy about a missed delivery. What price you quoted a client
last quarter, so you do not undercut yourself this quarter. Which quote is
three days from going stale with no follow-up sent.

That memory lives in one person's head. It does not survive a busy week,
a handover, or a new hire. This script is a prosthetic for that memory:
a flat-file (JSON), no-database, standard-library system that holds
accounts, SLA rules, proposals, pricing terms and risk flags, and forces
disciplined data entry because the whole system is only as good as what
you put into it.

DESIGN PRINCIPLES THAT SHAPE THE CODE BELOW
--------------------------------------------
1. SLA windows are set PER TIER, not per account, because relationship risk
   and commercial value cluster into a small number of bands (strategic /
   key / standard). Tiering also makes onboarding a new account a one-line
   decision ("which of these three does it look like") instead of a fresh
   negotiation every time.
2. "Handle with care" flags are surfaced at the TOP of every session, before
   anything else prints, because the cost of missing a fragile account is
   asymmetric: a stale report is an inconvenience, a mishandled McKinsey-type
   account is a lost account. Burying that in a report defeats the point.
3. Pricing terms are stored locally and looked up, not re-typed, because
   margin erosion in a professional-services business happens one
   inconsistent quote at a time, not in one dramatic mistake.
4. Every entry point validates its inputs and prints an explicit WARNING for
   sparse or stale data rather than silently accepting it. Garbage in,
   confident-looking garbage out is worse than an obvious gap.
5. Persistence is one JSON file. No server, no daemon, no migration system.
   This is meant to run on a laptop between meetings.

The seed data at the bottom is placeholder/demo data, clearly labelled, and
is provided ONLY to prove the system runs end to end. Replace it with your
own real accounts, real SLA terms and real pricing before relying on this
for actual decisions.
"""

# =============================================================================
# STEP 1 — IMPORTS
# -----------------------------------------------------------------------------
# Why: standard library only, plus dataclasses/enum/datetime/json which are
# all standard library in Python 3.7+. No pip install, no dependency drift,
# no reason this can't run on a bare laptop five years from now.
# =============================================================================
import json
import sys
import uuid
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


# =============================================================================
# STEP 2 — CONSTANTS AND FILE LOCATIONS
# -----------------------------------------------------------------------------
# Why a constant path: a single, predictable data file is what makes "flat
# JSON persistence" actually frictionless. No config file, no environment
# variable to forget. It lives next to the script unless overridden.
# =============================================================================
DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "sla_manager_data.json"
DATE_FMT = "%Y-%m-%d"

# Business-day-aware "hours" are overkill for a tool run by one person.
# We use calendar hours/days throughout and say so — precision here would
# be false precision, since weekends and public holidays vary by account
# (an embassy's national day is not on your calendar).


# =============================================================================
# STEP 3 — ENUMS
# -----------------------------------------------------------------------------
# Why enums instead of free-text strings: every downstream comparison
# (status == "sent" vs "Sent" vs "SENT") is a bug waiting to happen in a
# hand-maintained system. Enums make invalid states a loud Python error
# instead of a silent data-quality problem three months later.
# =============================================================================
class AccountTier(str, Enum):
    """
    Why tiers, not per-account SLA negotiation every time:
    - STRATEGIC: accounts where a miss risks the relationship itself
      (a McKinsey-type recurring high-standards account, a diplomatic
      mission running a formal RFQ process). Tightest response windows.
    - KEY: high commercial value, recurring SLA, but more resilient to an
      occasional slow day (a Gulf Energy-type daily/weekly lunch SLA plus
      conference bookings).
    - STANDARD: everything else — one-off enquiries, prospecting, low
      recurring volume. Loosest windows, because holding every account to
      strategic-tier response times burns you out for no commercial gain.
    """
    STRATEGIC = "strategic"
    KEY = "key"
    STANDARD = "standard"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    AWAITING_RESPONSE = "awaiting_response"
    WON = "won"
    LOST = "lost"
    EXPIRED = "expired"


class FlagType(str, Enum):
    """The taxonomy of things this system watches for automatically."""
    SLA_FIRST_RESPONSE_BREACH = "sla_first_response_breach"
    SLA_QUOTE_TURNAROUND_BREACH = "sla_quote_turnaround_breach"
    QUOTE_FOLLOW_UP_DUE = "quote_follow_up_due"
    QUOTE_STALE_NO_CONTACT = "quote_stale_no_contact"
    HANDLE_WITH_CARE = "handle_with_care"
    ACCOUNT_CONTACT_STALE = "account_contact_stale"
    SPARSE_DATA = "sparse_data"


class FlagSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# =============================================================================
# STEP 4 — SLA RULE LIBRARY (per tier, not per account)
# -----------------------------------------------------------------------------
# Why this is its own small dataclass rather than hard-coded numbers
# scattered through the flagging logic: SLA terms change (you renegotiate,
# you tighten standards after a miss). Keeping them in one lookup table
# means changing a number in one place changes behaviour everywhere,
# instead of hunting through conditional logic.
# =============================================================================
@dataclass
class SLARule:
    tier: AccountTier
    first_response_hours: int          # how fast you must acknowledge a new enquiry
    quote_turnaround_hours: int        # how fast a full quote/proposal must go out
    follow_up_cadence_days: int        # how often an unanswered quote gets chased
    quote_validity_days: int           # after this, an un-responded quote is "stale"
    contact_cadence_days: int          # max gap before a recurring account needs a check-in


# The default SLA library. This is policy, not code — edit these numbers to
# match your own commitments. They are intentionally tiered per the
# reasoning in Step 3.
DEFAULT_SLA_LIBRARY: dict[AccountTier, SLARule] = {
    AccountTier.STRATEGIC: SLARule(
        tier=AccountTier.STRATEGIC,
        first_response_hours=4,
        quote_turnaround_hours=24,
        follow_up_cadence_days=2,
        quote_validity_days=14,
        contact_cadence_days=7,
    ),
    AccountTier.KEY: SLARule(
        tier=AccountTier.KEY,
        first_response_hours=8,
        quote_turnaround_hours=48,
        follow_up_cadence_days=4,
        quote_validity_days=21,
        contact_cadence_days=14,
    ),
    AccountTier.STANDARD: SLARule(
        tier=AccountTier.STANDARD,
        first_response_hours=24,
        quote_turnaround_hours=72,
        follow_up_cadence_days=7,
        quote_validity_days=30,
        contact_cadence_days=30,
    ),
}


# =============================================================================
# STEP 5 — CORE DATA MODEL: Account, Proposal, PricingTerm, Flag
# -----------------------------------------------------------------------------
# Why dataclasses: they give us typed fields, a free __init__, and a
# straightforward asdict() for JSON serialisation, without pulling in an
# ORM or a database daemon we explicitly do not want.
# =============================================================================
@dataclass
class Account:
    account_id: str
    name: str
    tier: AccountTier
    primary_contact: str = ""
    contact_email: str = ""
    handle_with_care: bool = False
    care_reason: str = ""              # REQUIRED (validated) if handle_with_care is True
    last_contact_date: Optional[str] = None   # ISO date string, YYYY-MM-DD
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime(DATE_FMT))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Account":
        d = dict(d)
        d["tier"] = AccountTier(d["tier"])
        return Account(**d)


@dataclass
class PricingTerm:
    """
    A single agreed price/term, scoped either to one account (client-specific
    rate) or general (your standard rate card). Kept local and looked up so
    that a new quote is built FROM this table, not from memory or from
    copy-pasting a previous quote and hoping the numbers still hold.
    """
    term_id: str
    description: str
    unit_price_kes: float
    account_id: Optional[str] = None   # None = general rate card, applies to anyone
    unit: str = "unit"                 # e.g. "per person", "per hour", "per session"
    valid_from: str = field(default_factory=lambda: datetime.now().strftime(DATE_FMT))
    valid_until: Optional[str] = None  # None = open-ended
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PricingTerm":
        return PricingTerm(**d)

    def is_active(self, as_of: Optional[date] = None) -> bool:
        as_of = as_of or date.today()
        vf = datetime.strptime(self.valid_from, DATE_FMT).date()
        if as_of < vf:
            return False
        if self.valid_until:
            vu = datetime.strptime(self.valid_until, DATE_FMT).date()
            if as_of > vu:
                return False
        return True


@dataclass
class Proposal:
    proposal_id: str
    account_id: str
    title: str
    status: ProposalStatus = ProposalStatus.DRAFT
    created_date: str = field(default_factory=lambda: datetime.now().strftime(DATE_FMT))
    sent_date: Optional[str] = None
    quote_expiry_date: Optional[str] = None
    last_follow_up_date: Optional[str] = None
    follow_up_count: int = 0
    value_note_kes: Optional[float] = None   # placeholder until a real figure is entered
    pricing_term_ids: list = field(default_factory=list)  # references into the pricing library
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Proposal":
        d = dict(d)
        d["status"] = ProposalStatus(d["status"])
        return Proposal(**d)


@dataclass
class Flag:
    """
    A flag is the OUTPUT of the auto-flagging engine, not stored input.
    It is generated fresh on every run from the current state of accounts
    and proposals, which is deliberate — a flag that could go stale itself
    would defeat the purpose.
    """
    flag_type: FlagType
    severity: FlagSeverity
    account_id: str
    account_name: str
    message: str
    proposal_id: Optional[str] = None

    def render(self) -> str:
        sev_marker = {
            FlagSeverity.CRITICAL: "[CRITICAL]",
            FlagSeverity.WARNING: "[WARNING] ",
            FlagSeverity.INFO: "[INFO]    ",
        }[self.severity]
        return f"{sev_marker} {self.account_name:<28} {self.message}"


# =============================================================================
# STEP 6 — VALIDATION HELPERS
# -----------------------------------------------------------------------------
# Why this is a separate layer rather than inline checks: the system's
# output is only as good as input discipline, per the brief. Centralising
# validation means every entry point (CLI add, programmatic add, bulk load)
# gets the same warnings, instead of re-implementing checks ad hoc and
# drifting out of sync.
# =============================================================================
def validate_account_input(name: str, tier: AccountTier, contact_email: str,
                            handle_with_care: bool, care_reason: str) -> list:
    """Returns a list of human-readable warning strings. Does not raise —
    sparse data is allowed in, but never silently."""
    warnings = []
    if not name or not name.strip():
        raise ValueError("Account name is required — refusing to create an unnamed account.")
    if not contact_email:
        warnings.append(f"'{name}': no contact email on file. Follow-ups cannot be drafted "
                         f"without one.")
    if handle_with_care and not care_reason.strip():
        raise ValueError(
            f"'{name}' is flagged handle_with_care but has no care_reason. "
            f"A care flag with no reason is worse than no flag — it cannot be acted on "
            f"by anyone who did not set it. Provide the reason."
        )
    if tier == AccountTier.STRATEGIC and not handle_with_care:
        # Not an error — strategic accounts are not automatically fragile —
        # but worth a nudge since the two often correlate in practice.
        warnings.append(f"'{name}' is STRATEGIC tier but not marked handle_with_care. "
                         f"Confirm that's deliberate.")
    return warnings


def validate_proposal_input(title: str, account_id: str, accounts: dict) -> list:
    warnings = []
    if not title or not title.strip():
        raise ValueError("Proposal title is required.")
    if account_id not in accounts:
        raise ValueError(f"No account with id '{account_id}' exists. Create the account first "
                          f"— a proposal cannot float free of an account.")
    return warnings


def days_between(d1: date, d2: date) -> int:
    return (d2 - d1).days


def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, DATE_FMT).date()


# =============================================================================
# STEP 7 — THE MANAGER CLASS
# -----------------------------------------------------------------------------
# Why one class holding all state and behaviour, with clearly separated
# methods per function, rather than a pile of module-level functions passing
# dicts around: this mirrors how the tool will actually be used — load once,
# do several operations in a session, save once. It also gives the CLI layer
# a single object to depend on, which keeps the CLI thin.
# =============================================================================
class AccountSLAManager:
    def __init__(self, data_file: Path = DEFAULT_DATA_FILE,
                 sla_library: Optional[dict] = None):
        self.data_file = Path(data_file)
        self.sla_library = sla_library or DEFAULT_SLA_LIBRARY
        self.accounts: dict[str, Account] = {}
        self.proposals: dict[str, Proposal] = {}
        self.pricing_terms: dict[str, PricingTerm] = {}
        self._load()

    # -------------------------------------------------------------------
    # STEP 7a — PERSISTENCE (flat JSON, no daemon)
    # -------------------------------------------------------------------
    def _load(self) -> None:
        """Load state from the JSON file if it exists. A missing file is
        treated as 'fresh install', not an error — this is what makes the
        tool frictionless to start using."""
        if not self.data_file.exists():
            return
        raw = json.loads(self.data_file.read_text())
        self.accounts = {a["account_id"]: Account.from_dict(a)
                          for a in raw.get("accounts", [])}
        self.proposals = {p["proposal_id"]: Proposal.from_dict(p)
                           for p in raw.get("proposals", [])}
        self.pricing_terms = {t["term_id"]: PricingTerm.from_dict(t)
                               for t in raw.get("pricing_terms", [])}

    def save(self) -> None:
        """Write the full state back to disk. Called explicitly after any
        mutation — no auto-save-on-every-field-change, so a session of
        edits is one deliberate write, not a stream of disk I/O."""
        payload = {
            "accounts": [a.to_dict() for a in self.accounts.values()],
            "proposals": [p.to_dict() for p in self.proposals.values()],
            "pricing_terms": [t.to_dict() for t in self.pricing_terms.values()],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.data_file.write_text(json.dumps(payload, indent=2))

    # -------------------------------------------------------------------
    # STEP 7b — ACCOUNT MANAGEMENT
    # -------------------------------------------------------------------
    def add_account(self, name: str, tier: AccountTier, primary_contact: str = "",
                     contact_email: str = "", handle_with_care: bool = False,
                     care_reason: str = "", notes: str = "") -> Account:
        warnings = validate_account_input(name, tier, contact_email,
                                           handle_with_care, care_reason)
        for w in warnings:
            print(f"WARNING: {w}")
        account = Account(
            account_id=str(uuid.uuid4())[:8],
            name=name.strip(),
            tier=tier,
            primary_contact=primary_contact,
            contact_email=contact_email,
            handle_with_care=handle_with_care,
            care_reason=care_reason,
            last_contact_date=datetime.now().strftime(DATE_FMT),
            notes=notes,
        )
        self.accounts[account.account_id] = account
        return account

    def touch_contact(self, account_id: str, when: Optional[str] = None) -> None:
        """Record that you had contact with this account today (or on a
        given date). This is the single most important habit for keeping
        the 'stale contact' flag honest — it only works if you use it."""
        account = self._require_account(account_id)
        account.last_contact_date = when or datetime.now().strftime(DATE_FMT)

    def set_handle_with_care(self, account_id: str, reason: str) -> None:
        account = self._require_account(account_id)
        if not reason.strip():
            raise ValueError("A handle-with-care flag requires a reason — "
                              "this is what makes it actionable by someone else.")
        account.handle_with_care = True
        account.care_reason = reason

    def clear_handle_with_care(self, account_id: str) -> None:
        account = self._require_account(account_id)
        account.handle_with_care = False
        account.care_reason = ""

    def _require_account(self, account_id: str) -> Account:
        if account_id not in self.accounts:
            raise ValueError(f"No account with id '{account_id}'.")
        return self.accounts[account_id]

    # -------------------------------------------------------------------
    # STEP 7c — PRICING / TERMS LIBRARY
    # -------------------------------------------------------------------
    # Why a lookup-first workflow: the brief's core risk is margin erosion
    # through quote inconsistency. add_pricing_term is the ONLY way a price
    # enters the system; build_quote_lines below only ever reads from it.
    # There is deliberately no path that lets a quote invent a number.
    # -------------------------------------------------------------------
    def add_pricing_term(self, description: str, unit_price_kes: float,
                          account_id: Optional[str] = None, unit: str = "unit",
                          valid_until: Optional[str] = None, notes: str = "") -> PricingTerm:
        if unit_price_kes <= 0:
            raise ValueError(f"Unit price for '{description}' must be positive — "
                              f"got {unit_price_kes}. Refusing to store a zero or negative rate.")
        if account_id and account_id not in self.accounts:
            raise ValueError(f"Cannot scope a pricing term to unknown account '{account_id}'.")
        term = PricingTerm(
            term_id=str(uuid.uuid4())[:8],
            description=description.strip(),
            unit_price_kes=unit_price_kes,
            account_id=account_id,
            unit=unit,
            valid_until=valid_until,
            notes=notes,
        )
        self.pricing_terms[term.term_id] = term
        return term

    def find_pricing_terms(self, account_id: Optional[str] = None,
                            active_only: bool = True) -> list:
        """
        Account-specific terms are returned ahead of general ones for the
        same description, so a quote builder always prefers the negotiated
        client rate over the standard rate card when both exist — this is
        the mechanism that actually prevents 'carrying one account's rate
        into another's proposal' (and the reverse: undercutting a client
        who already agreed to a higher rate).
        """
        results = [t for t in self.pricing_terms.values()
                   if t.account_id in (None, account_id)]
        if active_only:
            results = [t for t in results if t.is_active()]
        results.sort(key=lambda t: (t.account_id is None, t.description.lower()))
        return results

    def build_quote_lines(self, account_id: str, descriptions: list) -> tuple:
        """
        Look up each requested line item against the pricing library and
        return (lines, missing). 'missing' surfaces anything with no stored
        rate, so a gap is visible immediately instead of someone typing in
        a guessed number to fill it.
        """
        self._require_account(account_id)
        available = self.find_pricing_terms(account_id=account_id)
        by_desc = {}
        for t in available:
            # account-scoped entries were sorted first, so this keeps the
            # client-specific rate when both a general and specific exist.
            by_desc.setdefault(t.description.lower(), t)

        lines, missing = [], []
        for desc in descriptions:
            term = by_desc.get(desc.lower())
            if term:
                lines.append(term)
            else:
                missing.append(desc)
        return lines, missing

    # -------------------------------------------------------------------
    # STEP 7d — PROPOSAL MANAGEMENT
    # -------------------------------------------------------------------
    def add_proposal(self, account_id: str, title: str,
                      value_note_kes: Optional[float] = None,
                      pricing_term_ids: Optional[list] = None,
                      notes: str = "") -> Proposal:
        validate_proposal_input(title, account_id, self.accounts)
        proposal = Proposal(
            proposal_id=str(uuid.uuid4())[:8],
            account_id=account_id,
            title=title.strip(),
            value_note_kes=value_note_kes,
            pricing_term_ids=pricing_term_ids or [],
            notes=notes,
        )
        self.proposals[proposal.proposal_id] = proposal
        return proposal

    def mark_sent(self, proposal_id: str, sent_date: Optional[str] = None) -> None:
        """Marking a proposal SENT is what starts its SLA clock. Also sets
        quote_expiry_date automatically from the account's tier SLA, so
        expiry is never a manual date someone forgets to set."""
        proposal = self._require_proposal(proposal_id)
        account = self._require_account(proposal.account_id)
        sent = sent_date or datetime.now().strftime(DATE_FMT)
        proposal.status = ProposalStatus.SENT
        proposal.sent_date = sent
        rule = self.sla_library[account.tier]
        expiry = parse_date(sent) + timedelta(days=rule.quote_validity_days)
        proposal.quote_expiry_date = expiry.strftime(DATE_FMT)
        self.touch_contact(account.account_id, sent)

    def log_follow_up(self, proposal_id: str, when: Optional[str] = None) -> None:
        proposal = self._require_proposal(proposal_id)
        proposal.last_follow_up_date = when or datetime.now().strftime(DATE_FMT)
        proposal.follow_up_count += 1
        proposal.status = ProposalStatus.AWAITING_RESPONSE
        self.touch_contact(proposal.account_id, proposal.last_follow_up_date)

    def close_proposal(self, proposal_id: str, won: bool) -> None:
        proposal = self._require_proposal(proposal_id)
        proposal.status = ProposalStatus.WON if won else ProposalStatus.LOST

    def _require_proposal(self, proposal_id: str) -> Proposal:
        if proposal_id not in self.proposals:
            raise ValueError(f"No proposal with id '{proposal_id}'.")
        return self.proposals[proposal_id]

    # -------------------------------------------------------------------
    # STEP 7e — AUTO-FLAGGING ENGINE (SLA misses + data hygiene)
    # -------------------------------------------------------------------
    # Why this runs fresh every time rather than being stored: a flag is a
    # judgement about the CURRENT gap between now and a deadline. Storing
    # "flagged" as a boolean on the record would require remembering to
    # re-check and clear it — recomputing from timestamps on every call is
    # both simpler and impossible to leave stale by accident.
    # -------------------------------------------------------------------
    def check_sla_breaches(self, as_of: Optional[date] = None) -> list:
        as_of = as_of or date.today()
        flags = []
        for proposal in self.proposals.values():
            account = self.accounts.get(proposal.account_id)
            if account is None:
                continue  # orphaned proposal — data integrity issue, not an SLA issue
            rule = self.sla_library[account.tier]

            # DRAFT proposals sitting un-sent past the quote-turnaround window
            # are effectively an SLA breach on first response, even though
            # no clock was explicitly started — the client is still waiting.
            if proposal.status == ProposalStatus.DRAFT:
                age_hours = (datetime.now() - datetime.strptime(
                    proposal.created_date, DATE_FMT)).total_seconds() / 3600
                if age_hours > rule.quote_turnaround_hours:
                    flags.append(Flag(
                        flag_type=FlagType.SLA_QUOTE_TURNAROUND_BREACH,
                        severity=FlagSeverity.CRITICAL,
                        account_id=account.account_id,
                        account_name=account.name,
                        proposal_id=proposal.proposal_id,
                        message=(f"'{proposal.title}' still DRAFT after "
                                 f"{age_hours:.0f}h (SLA: {rule.quote_turnaround_hours}h "
                                 f"for {account.tier.value} tier). Quote never left the door."),
                    ))

            # SENT/AWAITING_RESPONSE proposals past their quote_expiry_date
            # with no resolution — the quote has gone stale on the client's
            # desk, and worse, no one has followed up recently either.
            if proposal.status in (ProposalStatus.SENT, ProposalStatus.AWAITING_RESPONSE):
                if proposal.quote_expiry_date:
                    expiry = parse_date(proposal.quote_expiry_date)
                    if as_of > expiry:
                        flags.append(Flag(
                            flag_type=FlagType.QUOTE_STALE_NO_CONTACT,
                            severity=FlagSeverity.WARNING,
                            account_id=account.account_id,
                            account_name=account.name,
                            proposal_id=proposal.proposal_id,
                            message=(f"'{proposal.title}' expired {days_between(expiry, as_of)}d "
                                     f"ago with status {proposal.status.value}. Close it out "
                                     f"(won/lost) or re-quote — do not leave it open."),
                        ))

                # Follow-up cadence: has it been longer than the tier's
                # cadence since the last follow-up (or since sent, if none)?
                last_touch = parse_date(proposal.last_follow_up_date) or parse_date(proposal.sent_date)
                if last_touch:
                    gap = days_between(last_touch, as_of)
                    if gap >= rule.follow_up_cadence_days:
                        flags.append(Flag(
                            flag_type=FlagType.QUOTE_FOLLOW_UP_DUE,
                            severity=FlagSeverity.WARNING,
                            account_id=account.account_id,
                            account_name=account.name,
                            proposal_id=proposal.proposal_id,
                            message=(f"'{proposal.title}' last touched {gap}d ago "
                                     f"(cadence: {rule.follow_up_cadence_days}d for "
                                     f"{account.tier.value} tier). Follow up due."),
                        ))

        # Account-level contact staleness, independent of any proposal.
        for account in self.accounts.values():
            rule = self.sla_library[account.tier]
            last = parse_date(account.last_contact_date)
            if last is None:
                flags.append(Flag(
                    flag_type=FlagType.SPARSE_DATA,
                    severity=FlagSeverity.INFO,
                    account_id=account.account_id,
                    account_name=account.name,
                    message="No last_contact_date on file — cannot assess contact staleness. "
                            "Log a touch-point to activate this check.",
                ))
                continue
            gap = days_between(last, as_of)
            if gap >= rule.contact_cadence_days:
                flags.append(Flag(
                    flag_type=FlagType.ACCOUNT_CONTACT_STALE,
                    severity=FlagSeverity.WARNING,
                    account_id=account.account_id,
                    account_name=account.name,
                    message=(f"No recorded contact in {gap}d (cadence: "
                             f"{rule.contact_cadence_days}d for {account.tier.value} tier)."),
                ))

        return flags

    def generate_follow_up_drafts(self, as_of: Optional[date] = None) -> list:
        """
        Produces ready-to-send follow-up NUDGE TEXT for every proposal that
        is due per check_sla_breaches' QUOTE_FOLLOW_UP_DUE logic. This is a
        draft generator, not a sender — per the standing rule of never
        sending on someone's behalf, it stops at producing text for you to
        review and send yourself.
        """
        as_of = as_of or date.today()
        due_flags = [f for f in self.check_sla_breaches(as_of)
                     if f.flag_type == FlagType.QUOTE_FOLLOW_UP_DUE]
        drafts = []
        for f in due_flags:
            proposal = self.proposals[f.proposal_id]
            account = self.accounts[f.account_id]
            contact_name = account.primary_contact or "there"
            subject = f"Following up: {proposal.title}"
            body = (
                f"Hi {contact_name},\n\n"
                f"Following up on the proposal for \"{proposal.title}\" sent "
                f"{proposal.sent_date}. Keen to hear your thoughts, and happy to "
                f"walk through any of the detail or adjust scope if useful on your side.\n\n"
                f"Let me know a good time this week.\n"
            )
            drafts.append({
                "account": account.name,
                "proposal": proposal.title,
                "to": account.contact_email or "(no email on file — WARNING)",
                "subject": subject,
                "body": body,
            })
        return drafts

    # -------------------------------------------------------------------
    # STEP 7f — "HANDLE WITH CARE" SURFACE LAYER
    # -------------------------------------------------------------------
    # Why this is its own method rather than just another flag type mixed
    # into check_sla_breaches: handle-with-care accounts are a standing
    # relationship-risk condition, not an event with a deadline. It belongs
    # at the top of the console, every session, unconditionally — the
    # brief is explicit that this must not be buried in a report.
    # -------------------------------------------------------------------
    def get_handle_with_care_accounts(self) -> list:
        return [a for a in self.accounts.values() if a.handle_with_care]

    def print_session_banner(self) -> None:
        care_accounts = self.get_handle_with_care_accounts()
        print("=" * 72)
        print(" HANDLE WITH CARE — READ THIS FIRST")
        print("=" * 72)
        if not care_accounts:
            print(" (none currently flagged)")
        else:
            for a in care_accounts:
                open_props = [p for p in self.proposals.values()
                               if p.account_id == a.account_id
                               and p.status in (ProposalStatus.SENT, ProposalStatus.AWAITING_RESPONSE)]
                open_note = f" | {len(open_props)} open proposal(s)" if open_props else ""
                print(f"  * {a.name} [{a.tier.value}] — {a.care_reason}{open_note}")
        print("=" * 72)
        print()

    # -------------------------------------------------------------------
    # STEP 7g — FULL DASHBOARD (ties everything together)
    # -------------------------------------------------------------------
    def print_dashboard(self) -> None:
        self.print_session_banner()

        flags = self.check_sla_breaches()
        critical = [f for f in flags if f.severity == FlagSeverity.CRITICAL]
        warning = [f for f in flags if f.severity == FlagSeverity.WARNING]
        info = [f for f in flags if f.severity == FlagSeverity.INFO]

        print(f"SLA & DATA FLAGS  ({len(critical)} critical, {len(warning)} warning, "
              f"{len(info)} info)")
        print("-" * 72)
        if not flags:
            print("  Nothing outstanding. Clean board.")
        else:
            for f in sorted(flags, key=lambda x: x.severity.value):
                print("  " + f.render())
        print()

        drafts = self.generate_follow_up_drafts()
        print(f"FOLLOW-UP DRAFTS READY  ({len(drafts)})")
        print("-" * 72)
        if not drafts:
            print("  None due right now.")
        else:
            for d in drafts:
                print(f"  -> {d['account']}: \"{d['proposal']}\" -> {d['to']}")
        print()

        print(f"ACCOUNTS ON FILE: {len(self.accounts)}   "
              f"PROPOSALS ON FILE: {len(self.proposals)}   "
              f"PRICING TERMS ON FILE: {len(self.pricing_terms)}")
        print()


# =============================================================================
# STEP 8 — DEMO / SEED DATA
# -----------------------------------------------------------------------------
# THIS IS PLACEHOLDER DATA. Tiers, contacts and prices below are illustrative
# only, chosen to exercise every code path (a strategic care-flagged account,
# a key account with a breach, a standard account with clean data). Replace
# with real accounts and real agreed terms before using this for anything
# that matters.
# =============================================================================
def seed_demo_data(manager: AccountSLAManager) -> None:
    mckinsey = manager.add_account(
        name="[SAMPLE] McKinsey-type Recurring Account",
        tier=AccountTier.STRATEGIC,
        primary_contact="Placeholder Contact",
        contact_email="placeholder@example.com",
        handle_with_care=True,
        care_reason="PLACEHOLDER: high standards, has paused a recurring account over "
                    "quality before. Confirm real reason before relying on this flag.",
        notes="PLACEHOLDER account — replace with real client record.",
    )
    embassy = manager.add_account(
        name="[SAMPLE] Diplomatic Mission Account",
        tier=AccountTier.STRATEGIC,
        primary_contact="Placeholder Protocol Officer",
        contact_email="protocol@example.com",
        handle_with_care=True,
        care_reason="PLACEHOLDER: formal RFQ process, national-day event, zero tolerance "
                    "for protocol errors.",
    )
    key_account = manager.add_account(
        name="[SAMPLE] Gulf-type Energy Client",
        tier=AccountTier.KEY,
        primary_contact="Placeholder Ops Contact",
        contact_email="ops@example.com",
        notes="PLACEHOLDER: daily/weekly SLA, halal documentation matters.",
    )
    standard_account = manager.add_account(
        name="[SAMPLE] Standard Corporate Enquiry",
        tier=AccountTier.STANDARD,
        primary_contact="Placeholder Contact",
        contact_email="enquiry@example.com",
    )

    # Pricing/terms library — general rate card plus one client-specific override.
    manager.add_pricing_term("Conference day delegate package", 4500.0,
                              unit="per person", notes="PLACEHOLDER general rate")
    manager.add_pricing_term("Conference day delegate package", 4000.0,
                              account_id=key_account.account_id,
                              unit="per person",
                              notes="PLACEHOLDER negotiated rate for this account")
    manager.add_pricing_term("Formal plated dinner service", 8500.0, unit="per person")
    manager.add_pricing_term("Outside catering setup fee", 45000.0, unit="flat")

    # Proposals across the lifecycle to exercise the flagging engine.
    p1 = manager.add_proposal(mckinsey.account_id, "[SAMPLE] Weekly breakfast catering renewal",
                               notes="PLACEHOLDER")
    manager.mark_sent(p1.proposal_id, sent_date=(date.today() - timedelta(days=20)).strftime(DATE_FMT))
    # Deliberately not followed up — will trigger both follow-up-due and stale flags.

    p2 = manager.add_proposal(embassy.account_id, "[SAMPLE] National day outside catering RFQ",
                               notes="PLACEHOLDER")
    manager.mark_sent(p2.proposal_id, sent_date=(date.today() - timedelta(days=1)).strftime(DATE_FMT))
    manager.log_follow_up(p2.proposal_id, when=date.today().strftime(DATE_FMT))
    # Recently touched — should be clean.

    p3 = manager.add_proposal(key_account.account_id, "[SAMPLE] Q4 conference package",
                               notes="PLACEHOLDER")
    # Left in DRAFT past SLA on purpose — will trigger a critical breach flag.
    manager.proposals[p3.proposal_id].created_date = (
        date.today() - timedelta(days=5)).strftime(DATE_FMT)

    manager.add_proposal(standard_account.account_id, "[SAMPLE] One-off enquiry quote",
                          notes="PLACEHOLDER — left in draft, within SLA, should not flag yet.")

    manager.save()


# =============================================================================
# STEP 9 — CLI LAYER
# -----------------------------------------------------------------------------
# Why argparse subcommands rather than an interactive menu loop: this tool
# is meant to slot into a daily routine (run once, see the dashboard, maybe
# log one action, done) or be scriptable (cron, a shell alias). A menu loop
# fights both of those uses.
# =============================================================================
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Account SLA & Proposal Manager — operational memory for named accounts."
    )
    parser.add_argument("--data-file", default=str(DEFAULT_DATA_FILE),
                         help="Path to the JSON data file (default: sla_manager_data.json "
                              "next to this script).")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("dashboard", help="Print the full session dashboard (default if no command given).")
    sub.add_parser("init-demo", help="Seed the data file with clearly-labelled placeholder demo data.")
    sub.add_parser("care", help="Print only the handle-with-care banner.")
    sub.add_parser("follow-ups", help="Print ready-to-send follow-up drafts.")
    sub.add_parser("sla-check", help="Print only the SLA/data-hygiene flags.")

    p_acc = sub.add_parser("add-account", help="Add a new account.")
    p_acc.add_argument("name")
    p_acc.add_argument("--tier", choices=[t.value for t in AccountTier], required=True)
    p_acc.add_argument("--contact", default="")
    p_acc.add_argument("--email", default="")
    p_acc.add_argument("--care", action="store_true", help="Flag as handle-with-care.")
    p_acc.add_argument("--care-reason", default="", help="Required if --care is set.")

    p_price = sub.add_parser("add-price", help="Add a pricing/terms library entry.")
    p_price.add_argument("description")
    p_price.add_argument("unit_price_kes", type=float)
    p_price.add_argument("--account-id", default=None, help="Scope to one account; omit for general rate.")
    p_price.add_argument("--unit", default="unit")

    p_prop = sub.add_parser("add-proposal", help="Add a new proposal for an account.")
    p_prop.add_argument("account_id")
    p_prop.add_argument("title")

    p_sent = sub.add_parser("mark-sent", help="Mark a proposal as sent (starts its SLA clock).")
    p_sent.add_argument("proposal_id")

    p_touch = sub.add_parser("touch", help="Log a contact touch-point on an account today.")
    p_touch.add_argument("account_id")

    return parser


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    manager = AccountSLAManager(data_file=Path(args.data_file))

    command = args.command or "dashboard"

    try:
        if command == "dashboard":
            manager.print_dashboard()

        elif command == "init-demo":
            if manager.accounts or manager.proposals:
                print("Data file already has accounts/proposals — refusing to overwrite "
                      "with demo data. Point --data-file at a fresh path to try the demo.")
                return 1
            seed_demo_data(manager)
            print(f"Seeded placeholder demo data into {manager.data_file}")
            manager.print_dashboard()

        elif command == "care":
            manager.print_session_banner()

        elif command == "follow-ups":
            drafts = manager.generate_follow_up_drafts()
            if not drafts:
                print("No follow-ups due.")
            for d in drafts:
                print("=" * 60)
                print(f"To: {d['to']}")
                print(f"Subject: {d['subject']}")
                print()
                print(d["body"])

        elif command == "sla-check":
            flags = manager.check_sla_breaches()
            if not flags:
                print("Clean board — no flags.")
            for f in sorted(flags, key=lambda x: x.severity.value):
                print(f.render())

        elif command == "add-account":
            acc = manager.add_account(
                name=args.name, tier=AccountTier(args.tier),
                primary_contact=args.contact, contact_email=args.email,
                handle_with_care=args.care, care_reason=args.care_reason,
            )
            manager.save()
            print(f"Added account '{acc.name}' (id: {acc.account_id})")

        elif command == "add-price":
            term = manager.add_pricing_term(
                description=args.description, unit_price_kes=args.unit_price_kes,
                account_id=args.account_id, unit=args.unit,
            )
            manager.save()
            print(f"Added pricing term '{term.description}' @ KES {term.unit_price_kes:,.2f} "
                  f"{term.unit} (id: {term.term_id})")

        elif command == "add-proposal":
            prop = manager.add_proposal(account_id=args.account_id, title=args.title)
            manager.save()
            print(f"Added proposal '{prop.title}' (id: {prop.proposal_id})")

        elif command == "mark-sent":
            manager.mark_sent(args.proposal_id)
            manager.save()
            print(f"Proposal {args.proposal_id} marked SENT. SLA clock started.")

        elif command == "touch":
            manager.touch_contact(args.account_id)
            manager.save()
            print(f"Logged contact touch-point for account {args.account_id} today.")

        else:
            parser.print_help()
            return 1

    except ValueError as e:
        # Deliberate validation errors surface as a clean message, not a
        # traceback — this is a tool meant to be run under pressure between
        # meetings, not debugged.
        print(f"ERROR: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
