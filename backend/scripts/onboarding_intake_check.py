"""Check a DRAFT HVAC client config against the intake fields we need before it is pushed anywhere. Offline and deterministic.

    python backend/scripts/onboarding_intake_check.py path/to/draft.yaml

Input: the draft client YAML (the same keys as clients/_template.yaml) plus an optional top-level `intake:` block holding
facts that are not part of the live config but that someone must have collected and confirmed with the owner:

    intake:
      hours_confirmed: true            # the OWNER said these hours are right (a person asserts this; we cannot know)
      service_area: "Fairfax, Arlington, Alexandria, Loudoun"
      emergency_policy: "No heat/no cool: book soonest, ring owner. Gas smell/CO: 911 first, then ring owner."
      escalation_rules: "Ring owner cell first; if no answer take a message; backup: dispatcher 703-..."
      transfer_number_confirmed: true  # the owner confirmed this number rings a human who will answer
      written_summary_approved: true   # the owner approved, in writing, what the assistant will say
      carrier: "Verizon wireless"      # their phone system, to pick the forwarding method
      forwarding_mode: "no_answer"     # forward_all | no_answer | after_hours | dedicated_number
      assistant_number: "+1571..."     # our number for them, once provisioned (loop check)

Keep the `intake:` block OUT of clients/<id>.yaml: remove it before the file is copied there.

Output: BLOCK items (do not push or go live until fixed) and RISK items (read each one and decide; log the decision in
the onboarding note). Exit code 1 if any BLOCK, else 0. A clean run means the DRAFT is complete and consistent. It does not
prove the facts are TRUE (that is the owner's confirmation) and it does not test calls (that is certify_client.py).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ClientConfig  # noqa: E402

BLOCK, RISK = "BLOCK", "RISK"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_E164 = re.compile(r"^\+1\d{10}$")
_HVAC = re.compile(r"hvac|heating|cooling|air condition|furnace|\bac\b|heat pump", re.I)
_PLACEHOLDER = re.compile(r"REPLACE|lorem ipsum|\bTODO\b|\bTBD\b|\bXXX\b|example\.com|555-01\d\d|\+1555555\d{4}", re.I)
_MONEY = re.compile(r"\$\s?\d|\b\d+\s?(dollars|bucks)\b|\bfree\s+(estimate|diagnostic|service call|trip)|no\s+(trip|service)\s+(fee|charge)", re.I)
_DIY_SAFETY = re.compile(
    r"(shut|turn)\s+(off|the)\s+(the\s+)?(gas|furnace|breaker|power|pilot)|open\s+(the\s+|your\s+)?windows|re-?light|"
    r"reset\s+(the\s+|your\s+)?(breaker|furnace|thermostat)|check\s+(the\s+|your\s+)?(filter|thermostat|breaker|pilot)", re.I)
_GAS_CO = re.compile(r"\bgas\b|carbon monoxide|\bCO alarm", re.I)


@dataclass(frozen=True)
class Issue:
    level: str   # BLOCK | RISK
    code: str
    message: str


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _texts(cfg: dict) -> list[str]:
    out = [str(cfg.get("opening_line") or ""), str(cfg.get("extra_instructions") or "")]
    out += [str(f.get("a") or "") for f in (cfg.get("faqs") or []) if isinstance(f, dict)]
    out += [str(f.get("q") or "") for f in (cfg.get("faqs") or []) if isinstance(f, dict)]
    return out


def _open_windows(hours) -> list[tuple[str, str, str]]:
    return [(d, w[0], w[1]) for d in DAYS if isinstance((w := (hours or {}).get(d)), list) and len(w) == 2]


def check_intake(draft: dict) -> list[Issue]:
    """Pure function: a draft dict (client keys plus optional `intake`) -> issues, BLOCKs first. No I/O."""
    issues: list[Issue] = []

    def add(level: str, code: str, message: str) -> None:
        issues.append(Issue(level, code, message))

    if not isinstance(draft, dict):
        return [Issue(BLOCK, "not_a_mapping", "the draft must be a YAML mapping of client fields")]
    cfg = {k: v for k, v in draft.items() if k != "intake"}
    intake = draft.get("intake") or {}
    if not isinstance(intake, dict):
        add(BLOCK, "intake_shape", "`intake:` must be a mapping")
        intake = {}

    # 1. The live validator's own opinion (the same rules the server enforces on upload).
    validated: ClientConfig | None = None
    try:
        validated = ClientConfig.model_validate(cfg)
    except Exception as exc:  # pydantic.ValidationError: keep the first lines, they name the field
        first = "; ".join(line.strip() for line in str(exc).splitlines()[1:5] if line.strip())
        add(BLOCK, "invalid_config", f"the server would reject this config: {first[:300]}")

    # 2. Identity and mode: a paying client is never a demo or a throwaway.
    cid = str(cfg.get("client_id") or "")
    if _blank(cid) or cid == "REPLACE_ME":
        add(BLOCK, "client_id", "client_id is missing or still the template placeholder")
    elif cid.startswith(("prep_", "demo_", "zz_", "callkettle_")):
        add(BLOCK, "client_id_reserved", f"client_id {cid!r} uses a reserved demo/test prefix; a paying client must not")
    if cfg.get("demo_mode"):
        add(BLOCK, "demo_mode", "demo_mode is true: bookings are wiped daily and hand-off is simulated; never set on a paying client")
    if not _HVAC.search(str(cfg.get("vertical") or "")):
        add(RISK, "not_hvac", f"vertical {cfg.get('vertical')!r} does not look like HVAC; this checker's rules assume HVAC")

    # 3. A human who will really answer.
    phone = str(cfg.get("escalation_phone") or "")
    if not _E164.match(phone):
        add(BLOCK, "no_transfer_number", f"escalation_phone {phone!r} is not a +1XXXXXXXXXX number: urgent callers could not reach a human")
    elif re.fullmatch(r"\+1555555\d{4}", phone):
        add(BLOCK, "fictional_transfer_number", "escalation_phone is a fictional 555 number")
    elif intake.get("assistant_number") and phone == str(intake["assistant_number"]):
        add(BLOCK, "transfer_loop", "the transfer number is the assistant's own line: an urgent call would loop back into the assistant")
    elif intake.get("transfer_number_confirmed") is not True:
        add(RISK, "transfer_number_unconfirmed", "nobody has confirmed with the owner that this number rings a person who will answer")
    policy = (cfg.get("policy") or {}) if isinstance(cfg.get("policy"), dict) else {}
    if policy.get("can_transfer") is False:
        add(BLOCK, "transfer_disabled", "policy.can_transfer is false: a caller can never be put through to a person")

    # 4. Emergencies: gas/CO -> 911 is enforced in code; the owner's own emergency rules must still be written down.
    if _blank(intake.get("emergency_policy")):
        add(BLOCK, "no_emergency_policy", "intake.emergency_policy is missing: record what counts as an emergency for this shop and who rings first")
    if policy.get("emergency_action") == "message":
        add(RISK, "emergency_no_ring", "policy.emergency_action is 'message': after the 911 advice the owner is alerted but NOT rung; confirm the owner chose this")
    if _blank(intake.get("escalation_rules")):
        add(BLOCK, "no_escalation_rules", "intake.escalation_rules is missing: who rings first, what happens on no answer, any backup person")
    for text in _texts(cfg):
        if _GAS_CO.search(text) and "911" not in text and not _DIY_SAFETY.search(text):
            add(RISK, "gas_text_without_911", f"text mentions gas/CO without 911: {text[:80]!r}")
            break
    for text in _texts(cfg):
        if _DIY_SAFETY.search(text):
            add(RISK, "diy_safety_advice", f"text tells callers to do something themselves (the assistant must give no repair or safety steps beyond 911): {text[:80]!r}")
            break

    # 5. No price quoting.
    if policy.get("can_quote") is True:
        add(BLOCK, "can_quote", "policy.can_quote is true: the assistant may repeat prices; the rule for HVAC onboarding is no price quoting")
    if policy.get("can_state_dispatch_fee") is True:
        add(RISK, "dispatch_fee", "policy.can_state_dispatch_fee is true: allowed only if the owner approved the exact fee text in writing")
    money = next((t for t in _texts(cfg) if _MONEY.search(t)), None)
    if money:
        add(RISK, "money_in_text", f"a greeting, FAQ or instruction states or implies a price/fee: {money[:80]!r}")

    # 6. Hours.
    windows = _open_windows(cfg.get("business_hours"))
    if isinstance(cfg.get("business_hours"), dict) and not windows:
        add(BLOCK, "no_hours", "no day has opening hours")
    if intake.get("hours_confirmed") is not True:
        add(RISK, "hours_unconfirmed", "the owner has not confirmed these opening hours (set intake.hours_confirmed only after they say so)")
        default = {d: ["08:00", "17:00"] for d in DAYS[:5]}
        hours = cfg.get("business_hours") or {}
        if all(hours.get(d) == v for d, v in default.items()) and all(hours.get(d) == "closed" for d in ("sat", "sun")):
            add(RISK, "hours_look_like_template", "hours equal the template default (Mon-Fri 8-5): likely never asked")
    if not _blank(intake.get("hours_confirmed")) and intake.get("hours_confirmed") not in (True, False):
        add(RISK, "hours_flag_type", "intake.hours_confirmed should be true or false, not a string")

    # 7. Services and area.
    services = cfg.get("services") or []
    if not services:
        add(BLOCK, "no_services", "no services are configured")
    elif any(_PLACEHOLDER.search(str(s.get("name", ""))) for s in services if isinstance(s, dict)):
        add(BLOCK, "placeholder_service", "a service name is still a template placeholder")
    faq_area = any(re.search(r"area|serve|zip|county|city|cities", str(f.get("q", "")), re.I) for f in (cfg.get("faqs") or []) if isinstance(f, dict))
    if _blank(intake.get("service_area")):
        add(BLOCK if not faq_area else RISK, "no_service_area",
            "intake.service_area is missing" + (" (an FAQ mentions the area, but nobody recorded the owner's actual list)" if faq_area else ": the assistant would book calls the shop cannot take"))

    # 8. Placeholders and owner channels.
    if any(_PLACEHOLDER.search(t) for t in _texts(cfg)) or _PLACEHOLDER.search(str(cfg.get("business_name") or "")):
        add(BLOCK, "placeholder_text", "the greeting, name, instructions or FAQs still contain placeholder text (REPLACE / TODO / TBD / example.com / 555 numbers)")
    if _blank(cfg.get("owner_email")) and _blank(cfg.get("ntfy_topic")):
        add(BLOCK, "no_owner_channel", "neither owner_email nor ntfy_topic is set: the owner would never hear about a booking or an urgent call")
    if not (cfg.get("calendar_ical_url") or cfg.get("google_calendar_id")):
        add(RISK, "no_calendar", "no calendar connected: the assistant cannot see the owner's own jobs and may offer a taken time (say so to the owner)")
    if intake.get("written_summary_approved") is not True:
        add(RISK, "summary_not_approved", "the owner has not approved the written summary of what the assistant will say")

    # 9. Phone wiring facts.
    if _blank(intake.get("carrier")) or _blank(intake.get("forwarding_mode")):
        add(RISK, "forwarding_unplanned", "carrier or forwarding_mode is missing: the forwarding method cannot be chosen yet (docs/TELEPHONY.md section 4)")
    if intake.get("forwarding_mode") == "forward_all" and (cfg.get("routing_mode") or "ai_first") != "ai_first":
        add(BLOCK, "routing_loop", "routing_mode rings the owner first but the owner forwards ALL calls to us: the ring would loop back")
    if cfg.get("transfer_screening") is True:
        add(RISK, "screening_unverified", "transfer_screening is on: it is OFF until verified with a real phone (docs/TELEPHONY.md section 7)")

    return sorted(issues, key=lambda i: (i.level != BLOCK, i.code))


def render(issues: list[Issue], label: str = "draft") -> str:
    blocks = [i for i in issues if i.level == BLOCK]
    risks = [i for i in issues if i.level == RISK]
    lines = [f"Intake check: {label}", f"{len(blocks)} BLOCK, {len(risks)} RISK", ""]
    for i in issues:
        lines.append(f"[{i.level}] {i.code}: {i.message}")
    if not issues:
        lines.append("No missing or risky items found in the draft. This does not verify the facts are true or test any call.")
    elif blocks:
        lines += ["", "Do not push or go live until every BLOCK is fixed."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    path = Path(args[0])
    try:
        draft = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2
    issues = check_intake(draft)
    print(render(issues, path.name))
    return 1 if any(i.level == BLOCK for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
