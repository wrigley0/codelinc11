"""API contract for the dental prototype.

This file is the single source of truth for request/response shapes.
Only the orchestrator / API owner changes it. Everyone else imports from it.
All money values are floats in US dollars, rounded to 2 decimals by the engine.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["preventive", "basic", "major"]
Urgency = Literal["urgent", "soon", "flexible"]


# ---------- Catalog data ----------

class Plan(BaseModel):
    id: str
    name: str
    description: str = ""
    monthly_premium: float = 0
    deductible: float                                   # per person per plan year
    deductible_waived_for: list[Category] = ["preventive"]
    annual_max: float
    coinsurance: dict[Category, float]                  # PLAN's share, e.g. {"basic": 0.8}
    frequency: dict[str, int] = {}                      # max per plan year by CDT code, e.g. {"D1110": 2}
    plan_year_start_month: int = 1                      # 1 = January
    orthodontia_child: float = 0.0                      # plan's share for children's braces; shown on the Plans page, not used by the engine yet
    alternate_benefit: bool = False                     # plan pays a costlier option at the price of its cheaper alternative


class Procedure(BaseModel):
    code: str                                           # CDT code, e.g. "D2740"
    name: str
    category: Category
    description: str = ""
    synonyms: list[str] = []
    fee_p50: float                                      # typical in-network (allowed) amount
    fee_p80: float                                      # typical out-of-network billed amount
    alternative_codes: list[str] = []                   # cheaper clinically-acceptable alternatives (CDT codes)


class Usage(BaseModel):
    """What the member has already used in the CURRENT plan year."""
    max_used: float = 0
    deductible_met: float = 0
    history: list[str] = []                             # CDT codes already done this plan year


# ---------- Estimate ----------

class TraceStep(BaseModel):
    label: str
    amount: float
    note: str


class EstimateResult(BaseModel):
    code: str
    name: str
    category: Category
    in_network: bool
    covered: bool                                       # False when a frequency limit blocks coverage
    billed: float                                       # what the dentist charges
    allowed: float                                      # amount the plan bases payment on
    deductible_applied: float
    plan_pays: float
    you_pay: float
    balance_bill: float                                 # billed - allowed (0 in network)
    max_used_after: float                               # plan-year max used after this procedure
    trace: list[TraceStep]


class EstimateRequest(BaseModel):
    plan_id: str | None = None
    plan: Plan | None = None                         # custom plan (overrides plan_id)
    code: str
    usage: Usage = Usage()


class EstimateResponse(BaseModel):
    in_network: EstimateResult
    out_of_network: EstimateResult


# ---------- Procedure search ----------

class ProcedureMatch(BaseModel):
    procedure: Procedure
    score: float                                        # 0..1, higher is better


# ---------- Plan My Year ----------

class TreatmentItem(BaseModel):
    id: str                                             # unique within the request, e.g. "t1"
    code: str
    urgency: Urgency = "flexible"
    after: str | None = None                         # id of an item that must happen first


class ScheduleRequest(BaseModel):
    plan_id: str | None = None
    plan: Plan | None = None
    items: list[TreatmentItem]
    usage: Usage = Usage()
    current_month: int = Field(default=11, ge=1, le=12)  # month we are in now (1-12)


class ScheduledItem(BaseModel):
    id: str
    code: str
    name: str
    category: Category
    year_offset: int                                    # 0 = current plan year, 1 = next plan year
    month: int                                          # 1-12
    plan_pays: float
    you_pay: float
    note: str                                           # plain-English reason for this placement


class YearSummary(BaseModel):
    year_offset: int
    label: str                                          # e.g. "This plan year" / "Next plan year"
    plan_pays: float
    you_pay: float
    max_used_end: float
    max_remaining_end: float


class ScheduleResponse(BaseModel):
    items: list[ScheduledItem]                          # ordered chronologically
    years: list[YearSummary]
    total_you_pay: float                                # optimized schedule
    baseline_you_pay: float                             # everything done now (this plan year)
    savings: float                                      # baseline - total (never negative)
    baseline_items: list[ScheduledItem]                 # the "everything now" schedule, for the toggle
    reasons: list[str]                                  # plain-English bullets explaining the schedule


# ---------- Benefits status ----------

class BenefitsStatusRequest(BaseModel):
    plan_id: str | None = None
    plan: Plan | None = None
    usage: Usage = Usage()
    current_month: int = Field(default=11, ge=1, le=12)


class FrequencyStatus(BaseModel):
    code: str
    name: str
    used: int
    limit: int
    remaining: int


class BenefitsStatus(BaseModel):
    plan_name: str
    annual_max: float
    max_used: float
    max_remaining: float
    deductible: float
    deductible_met: float
    deductible_remaining: float
    frequencies: list[FrequencyStatus]
    unused_preventive_value: float                      # fee value of covered-but-unused preventive visits
    months_left: int                                    # months left in the plan year, including this one
    reminder: str | None = None                      # set when benefits would expire soon (month >= 10)


# ---------- Chat ----------

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(max_length=20)
    plan_id: str | None = None
    plan: Plan | None = None
    usage: Usage = Usage()
    current_month: int = Field(default=11, ge=1, le=12)


class HealthResponse(BaseModel):
    ok: bool
    ollama_available: bool
    ollama_model: str
    chat_mode: Literal["anthropic", "ollama", "unavailable"]


# ---------- Treatment plan (dentist quote) parsing ----------

class ParsedTreatment(BaseModel):
    id: str                                             # "q1", "q2", ... unique within the response
    code: str | None = None                          # catalog CDT code, None when not matched
    name: str                                           # catalog name if matched, else the text found
    tooth: str | None = None                         # e.g. "19"
    quoted_fee: float | None = None                  # fee written on the dentist's plan
    typical_fee: float | None = None                 # catalog fee_p50 when matched
    urgency: Urgency = "flexible"
    after: str | None = None                         # id of a ParsedTreatment that must come first
    phase: str | None = None                         # e.g. "Phase 1"
    matched: bool = False
    confidence: float = 0.0                             # 0..1
    source_line: str = ""


class TreatmentPlanParseRequest(BaseModel):
    text: str
    plan_id: str | None = None
    plan: Plan | None = None


class ProviderMatch(BaseModel):
    """Which directory practice a pasted dentist quote came from (sprint 2, B4), found by matching the
    quote's header (practice name, phone, dentist and ZIP) against the providers table."""
    matched: bool
    provider_id: str | None = None
    name: str | None = None                             # practice name
    dentist: str | None = None
    address: str | None = None                          # "street, city, ST zip"
    in_network: bool | None = None                      # for plan_id when it is a plan tier; False when unmatched
    network_note: str                                   # plain language; says so when we priced out of network
    source: Literal["insurer directory"] = "insurer directory"


class TreatmentPlanParseResponse(BaseModel):
    items: list[ParsedTreatment]
    unmatched_lines: list[str] = []                     # lines that looked like treatments but could not be matched
    notes: list[str] = []                               # plain-English notes for the user
    mode: Literal["rules", "ollama"] = "rules"
    provider_match: ProviderMatch | None = None         # always set by POST /treatment-plan/parse


class TreatmentPlanSample(BaseModel):
    """A synthetic dentist quote to try (GET /treatment-plan/samples)."""
    id: str
    title: str
    description: str                                    # one plain sentence about what it shows
    expected_network: Literal["in", "out", "unknown"]   # for the Preferred plan
    text: str


# ---------- Questions to ask your dentist ----------

class DentistQuestion(BaseModel):
    id: str
    text: str
    why: str                                            # one-line reason this question matters


class QuestionSection(BaseModel):
    id: str
    title: str                                          # e.g. "Is it urgent?", "Cost and billing"
    questions: list[DentistQuestion]


class QuestionsRequest(BaseModel):
    plan_id: str | None = None
    plan: Plan | None = None
    codes: list[str] = []                               # procedures being considered (may be empty)
    usage: Usage = Usage()
    current_month: int = Field(default=11, ge=1, le=12)


class QuestionsResponse(BaseModel):
    safety_note: str                                    # always present: never delay urgent/painful care
    sections: list[QuestionSection]


# ---------- Savings tips ----------

TipKind = Literal["timing", "network", "preventive", "alternative", "fsa_hsa", "quote_check"]


class SavingsTip(BaseModel):
    id: str
    kind: TipKind
    title: str
    summary: str                                        # plain-English, one or two sentences
    saving: float                                       # dollars; computed by the engine, never by the LLM
    before: float                                       # cost without following the tip
    after: float                                        # cost if the tip is followed
    steps: list[TraceStep] = []                         # how the number was calculated
    assumptions: list[str] = []


class SavingsTipsRequest(BaseModel):
    plan_id: str | None = None
    plan: Plan | None = None
    usage: Usage = Usage()
    current_month: int = Field(default=11, ge=1, le=12)
    items: list[TreatmentItem] = []                     # treatments under consideration
    quoted_fees: dict[str, float] = {}                  # treatment item id -> fee on the dentist's quote
    tax_rate: float = Field(default=0.25, ge=0, le=0.6) # assumed marginal tax rate for FSA/HSA tip


class SavingsTipsResponse(BaseModel):
    tips: list[SavingsTip]                              # sorted by saving, largest first; tips overlap, never sum them
    note: str = "Tips overlap, so their savings can't be added together. These are estimates, not tax or medical advice."


# ---------- Households, sign-in and overview (T05) ----------

class DemoAccount(BaseModel):
    account_id: str
    email: str
    display_name: str
    member_id: str
    role: Literal["primary", "adult", "managed"]
    household_id: str
    status: str = "active"                              # "pending" while eligibility is unconfirmed


class DemoLoginRequest(BaseModel):
    member_id: str                                      # a template id e.g. "m-alex" (or a sandbox id)
    sandbox: bool = False                               # true: sign in to the visitor's own demo family
    household_id: str | None = Field(default=None, max_length=64)   # reuse this sandbox (needs sandbox true)


class Member(BaseModel):
    id: str
    household_id: str
    name: str
    relationship: str
    age: int
    role: Literal["primary", "adult", "managed"]
    has_login: bool
    status: str = "active"                              # "active" or "pending"
    status_note: str | None = None
    # Profile fields (sprint 2, B1). All optional so older clients keep working.
    dob: str | None = None                              # ISO date; `age` is derived from it
    email: str | None = None                            # contact email (not the sign-in account)
    phone: str | None = None                            # digits only, e.g. "3345550142"
    zip: str | None = None                              # 5 digits
    notes: str | None = None                            # up to 200 characters
    primary_dentist_id: str | None = None               # set by the providers feature


class ProfilePatch(BaseModel):
    """PATCH /members/{id}/profile. Send only what changes; null or "" clears email, phone, zip, notes.
    Values are checked by the route (plain 422 messages)."""
    name: str | None = Field(default=None, max_length=200)
    dob: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=60)
    zip: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)
    primary_dentist_id: str | None = Field(default=None, max_length=64)   # a provider id; null or "" clears


class NewMember(BaseModel):
    """POST /households/{id}/members."""
    name: str = Field(max_length=200)
    relationship: Literal["spouse", "partner", "child", "other"]
    dob: str = Field(max_length=40)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=60)
    zip: str | None = Field(default=None, max_length=40)


class PlanTierSummary(BaseModel):
    id: str
    name: str
    monthly_premium: float                              # dollars per covered person per month
    annual_max: float
    deductible: float
    preventive_pct: int
    basic_pct: int
    major_pct: int
    ortho_pct: int


class Household(BaseModel):
    id: str
    name: str
    plan_tier: PlanTierSummary
    members: list[Member]                               # only the members the signed-in person may see


class SandboxInfo(BaseModel):
    household_id: str                                   # the sandbox household id, e.g. "hh-rivera.3f9a1c"
    expires_at: str                                     # ISO 8601 UTC


class DemoLoginResponse(BaseModel):
    token: str                                          # send as "Authorization: Bearer <token>"
    member: Member
    household: Household
    sandbox: SandboxInfo | None = None                  # set when signed in to a sandbox


class MemberName(BaseModel):
    member_id: str = Field(max_length=64)
    name: str = Field(max_length=200)


class HouseholdNamesRequest(BaseModel):
    """Rename a demo family. Names are checked by the route (letters, spaces, ' - . only)."""
    household_name: str | None = Field(default=None, max_length=200)   # surname, e.g. "Demo"
    members: list[MemberName] = Field(default_factory=list, max_length=12)


class ServiceEligibility(BaseModel):
    service: Literal["preventive", "basic", "major", "orthodontia"]
    label: str
    covered: bool
    plan_share: float                                   # plan's share, 0..1
    deductible_applies: bool
    note: str


class MemberUsageDollars(BaseModel):
    plan_year: int
    max_used: float
    deductible_met: float
    visits: int
    cleanings_used: int


class VisitRequest(BaseModel):
    code: str                                           # catalog procedure code
    in_network: bool = True
    visit_date: date | None = None                      # default: the demo "today"


class VisitResponse(BaseModel):
    estimate: EstimateResult                            # what the engine worked out for this visit
    usage: MemberUsageDollars                           # this person's usage after the visit
    benefits: BenefitsStatus                            # benefits status after the visit


class MemberOverview(BaseModel):
    member: Member
    plan_tier: PlanTierSummary
    usage: MemberUsageDollars                           # this person's usage only, never a household total
    benefits: BenefitsStatus                            # computed by the benefits-status engine
    reminder: str | None = None
    eligibility: list[ServiceEligibility]
    as_of: str                                          # demo date used as "today" (ISO)
    notifications_unread: int = 0                       # unread in-app notifications (0 when the app channel is off)


class ScheduleEntry(BaseModel):
    id: int
    member_id: str
    member_name: str
    kind: str                                           # "appointment" or "reminder"
    due_date: str
    title: str
    note: str | None = None


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    member_id: str | None = None


class InviteResponse(BaseModel):
    id: str
    household_id: str
    invited_by: str
    member_id: str | None = None
    email: str
    status: str
    created_at: str


# ---------- Annual cost calculator ----------

class CareItem(BaseModel):
    code: str
    count: int = Field(default=1, ge=1, le=12)


class AnnualCostRequest(BaseModel):
    tier_id: str                                        # "basic" | "preferred" | "premium"
    covered_people: int = Field(default=1, ge=1, le=10)
    expected_care: list[CareItem] = Field(default=[], max_length=30)  # per person, applied to each covered person
    in_network: bool = True


class AnnualCostPerson(BaseModel):
    person: int                                         # 1-based
    plan_pays: float
    you_pay: float
    max_used_end: float


class AnnualCostResponse(BaseModel):
    tier_id: str
    tier_name: str
    covered_people: int
    premiums: float                                     # monthly premium x 12 x covered people
    plan_pays: float
    out_of_pocket_care: float
    total_cost: float                                   # premiums + out_of_pocket_care
    per_person: list[AnnualCostPerson]
    assumptions: list[str]
    disclaimer: str = "This is an estimate. Your actual cost depends on your dentist's charges and claim review."


# ---------- Choose a Plan: Monte Carlo plan comparison (F7) ----------

CareLevel = Literal["low", "average", "high"]


class SimulateKnownCare(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    count: int = Field(default=1, ge=1, le=5)


class SimulateMember(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    age: int = Field(ge=0, le=120)
    care_level: CareLevel = "average"
    known_care: list[SimulateKnownCare] = Field(default=[], max_length=10)  # added to every simulated year


class SimulateRequest(BaseModel):
    members: list[SimulateMember] = Field(min_length=1, max_length=8)
    plan_ids: list[str] | None = Field(default=None, max_length=10)       # default: every plan
    n: int = Field(default=5000, ge=100, le=20000)                         # simulated years
    seed: int = 42
    in_network: bool = True


class SimulatePlanResult(BaseModel):
    plan_id: str
    name: str
    monthly_premium: float
    premiums_total: float                               # monthly premium x 12 x people
    mean: float                                         # household total: premiums + what the family pays
    median: float                                       # nearest-rank 50th percentile
    p10: float
    p90: float
    min: float
    max: float
    cheapest_share: int                                 # whole percent; shares add up to exactly 100
    histogram: list[int]                                # one count per bin, shared bin_edges


class SimulateResponse(BaseModel):
    n: int
    seed: int
    in_network: bool
    plans: list[SimulatePlanResult]
    bin_edges: list[float]
    winner_plan_id: str
    reasons: list[str]
    assumptions: list[str]
    disclaimer: str = "This is an estimate. Your actual cost depends on your dentist's charges and claim review."


# ---------- Saved "Which plan fits us?" comparisons (T34) ----------

class SavedSimulationPlanSummary(BaseModel):
    plan_id: str
    name: str
    cheapest_share: int
    median: float
    p90: float


class SavedSimulationSummary(BaseModel):
    winner_plan_id: str
    winner_name: str
    winner_share: int
    plans: list[SavedSimulationPlanSummary]
    current_plan_id: str | None = None


class SavedSimulationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    request: SimulateRequest


class SavedSimulationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    request: SimulateRequest | None = None


class SavedSimulation(BaseModel):
    id: str
    member_id: str
    name: str
    request: SimulateRequest
    summary: SavedSimulationSummary
    created_at: str
    updated_at: str


# ---------- Notifications (sprint 2, B2) ----------

NotificationKind = Literal["benefits_expiring", "preventive_unused", "upcoming_appointment",
                           "reminder", "procedure_planned", "deductible_met", "claim_update", "eob_ready", "test"]


class Notification(BaseModel):
    id: int
    member_id: str
    kind: NotificationKind
    title: str
    body: str
    severity: Literal["info", "success", "warning"]
    link: str | None = None                             # app route to open, for example "/plan-year"
    created_at: str                                     # ISO time (UTC)
    read_at: str | None = None                          # null while unread


class NotificationList(BaseModel):
    notifications: list[Notification]                   # newest first
    unread_count: int                                   # all unread, even when `unread=1` is not used
    app_enabled: bool = True                            # false: the person turned the in-app channel off


class NotificationPrefs(BaseModel):
    app: bool = True
    email: bool = False
    sms: bool = False
    types: list[NotificationKind] | None = None         # null means every kind
    email_on_file: bool = False                         # whether a valid email is saved (the address is not returned)
    phone_on_file: bool = False


class NotificationPrefsUpdate(BaseModel):
    app: bool = True
    email: bool = False
    sms: bool = False
    types: list[NotificationKind] | None = None


class TestNotificationRequest(BaseModel):
    channel: Literal["app", "email", "sms"]


class OutboxMessage(BaseModel):
    id: int
    member_id: str
    channel: Literal["email", "sms", "push"]
    to_address: str                                     # the stored contact (shown only to people who may see this member)
    subject: str | None = None
    body: str
    created_at: str
    status: Literal["preview", "queued", "sent", "failed"] = "preview"
    provider_message_id: str | None = None
    error: str | None = None


class TestNotificationResponse(BaseModel):
    ok: bool = True
    channel: Literal["app", "email", "sms"]
    notification: Notification | None = None           # for channel "app"
    outbox: OutboxMessage | None = None                 # for "email" and "sms" (a preview, not sent)


# ---------- Web push subscriptions (migration 010) ----------

class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    """A browser PushSubscription, as returned by the Push API in the frontend."""
    endpoint: str
    keys: PushKeys


class PushConfig(BaseModel):
    """What the frontend needs to subscribe. enabled is false when web push isn't configured."""
    enabled: bool
    public_key: str | None = None


# ---------- Providers (sprint 2, B3) ----------

class ProviderEstimate(BaseModel):
    """What one procedure would cost this member at this provider (engine numbers)."""
    you_pay: float
    plan_pays: float
    in_network: bool
    balance_bill: float                                 # 0 in network
    note: str                                           # plain language, says fees are not provider specific


class ProviderOut(BaseModel):
    id: str
    practice_name: str
    dentist_name: str
    specialty: Literal["general", "pediatric", "orthodontics", "oral_surgery", "endodontics", "periodontics"]
    address: str
    city: str
    state: str
    zip: str
    lat: float
    lon: float
    phone: str
    accepting_new: bool
    languages: list[str]
    network_plan_ids: list[str]
    distance_mi: float | None = None                    # miles from the searched ZIP, one decimal
    in_network: bool                                    # for the household's CURRENT plan
    estimate: ProviderEstimate | None = None           # only when `code` is given


# ---------- Reports: claims, EOBs, copays (sprint 2, B4). SYNTHETIC documents only ----------

ReportKind = Literal["claim", "eob", "copay", "other"]
PaidStatus = Literal["unpaid", "paid", "not_applicable"]


class ReportSample(BaseModel):
    id: str                                             # e.g. "sample-eob-deductible"
    title: str
    kind: ReportKind
    description: str                                    # one plain sentence about what it shows
    text: str                                           # the sample template; also what upload accepts


class ReportData(BaseModel):
    """Fields of one document, exactly as stored (dollars). Only the ones that apply are filled."""
    claim_number: str | None = None
    eob_number: str | None = None
    status: Literal["paid", "denied", "pending"] | None = None   # claims
    billed: float | None = None
    allowed: float | None = None
    deductible_applied: float | None = None
    coinsurance_amount: float | None = None
    copay_amount: float | None = None
    plan_paid: float | None = None
    you_owe: float | None = None
    balance_billing: float | None = None
    remark: str | None = None


class ReportItem(BaseModel):
    id: str
    member_id: str
    kind: ReportKind
    service_date: str                                   # ISO date
    title: str
    provider_id: str | None = None                      # set when the practice is in the directory
    provider_name: str
    code: str | None = None
    description: str = ""
    data: ReportData
    paid_status: PaidStatus
    created_at: str


class ReportTotals(BaseModel):
    """Sums of stored values over the listed items (see engine/reports.py)."""
    billed: float
    allowed: float
    plan_paid: float
    you_paid: float                                     # you owe on items marked paid
    you_owe_open: float                                 # you owe on items not yet marked paid


class ReportList(BaseModel):
    items: list[ReportItem]
    totals: ReportTotals
    count: int


class ReportLine(BaseModel):
    label: str                                          # e.g. "Billed"
    amount: float | None = None
    plain: str                                          # what the line means, in plain words


class ReportStep(BaseModel):
    key: Literal["billed", "allowed", "deductible", "plan_paid", "you_owe"]
    label: str
    amount: float
    plain: str


class ReportExplanation(BaseModel):
    """Built in code from the stored fields. No model is involved and no amount is recomputed."""
    id: str
    kind: ReportKind
    title: str
    what_it_is: str
    lines: list[ReportLine]
    steps: list[ReportStep]                             # billed, allowed, deductible, plan paid, you owe (EOB, copay)
    what_to_do_next: list[str]
    balance_billing_note: str | None = None             # only when the dentist is out of network
    lines_add_up: bool
    synthetic_notice: str
    disclaimer: str
