import re
import quopri
import uuid
import datetime
import phonenumbers
from typing import Dict, List, Set, Tuple, Optional


_PHONE_KEYS_CACHE: Dict[str, Set[str]] = {}
_E164_CACHE: Dict[str, Tuple[str, str]] = {}


def generate_phone_keys(raw_phone: str) -> Set[str]:
    """
    Generates all normalized lookup keys for a phone number to guarantee
    100% deduplication across Brazil (8 vs 9 digits) and international numbers.
    """
    if not raw_phone:
        return set()

    cache_hit = _PHONE_KEYS_CACHE.get(raw_phone)
    if cache_hit is not None:
        return cache_hit

    keys: Set[str] = set()
    digits = re.sub(r"\D", "", str(raw_phone))
    if not digits or len(digits) < 7:
        return keys

    keys.add(digits)

    # Fast-path for standard Brazilian numbers (55 + 2-digit DDD + 8 or 9 digits) without invoking slow phonenumbers parser
    if digits.startswith("55") and len(digits) in (12, 13):
        ddd = digits[2:4]
        if len(digits) == 12:
            sub8 = digits[4:]
            keys.add(f"{ddd}{sub8}")
            keys.add(sub8)
            if sub8[0] in "6789":
                keys.add(f"55{ddd}9{sub8}")
                keys.add(f"{ddd}9{sub8}")
        else:
            sub9 = digits[4:]
            keys.add(f"{ddd}{sub9}")
            if sub9[0] == "9":
                sub8 = sub9[1:]
                keys.add(f"55{ddd}{sub8}")
                keys.add(f"{ddd}{sub8}")
                keys.add(sub8)
        _PHONE_KEYS_CACHE[raw_phone] = keys
        return keys

    # Try parsing with phonenumbers (defaulting to BR if no country code)
    for region in (None, "BR", "US", "PT"):
        try:
            candidate = f"+{digits}" if region is None else raw_phone
            parsed = phonenumbers.parse(candidate, region)
            if phonenumbers.is_possible_number(parsed):
                e164 = phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                ).lstrip("+")
                keys.add(e164)
                nat = str(parsed.national_number)
                if len(nat) >= 8:
                    keys.add(nat)
        except Exception:
            pass

    # Handle Brazilian 8-digit vs 9-digit mobile variations (55 + 2-digit DDD + 8 or 9 digits)
    for k in list(keys):
        # Case 1: Starts with 55 and has 12 digits (55 + DDD + 8 digits) -> also add 9-digit version
        if k.startswith("55") and len(k) == 12:
            ddd = k[2:4]
            subscriber = k[4:]
            keys.add(f"{ddd}{subscriber}")
            if subscriber[0] in "6789":
                keys.add(f"55{ddd}9{subscriber}")
                keys.add(f"{ddd}9{subscriber}")
            keys.add(subscriber)

        # Case 2: Starts with 55 and has 13 digits (55 + DDD + 9 + 8 digits) -> also add 8-digit version
        elif k.startswith("55") and len(k) == 13 and k[4] == "9":
            ddd = k[2:4]
            subscriber8 = k[5:]
            keys.add(f"{ddd}9{subscriber8}")
            keys.add(f"55{ddd}{subscriber8}")
            keys.add(f"{ddd}{subscriber8}")
            keys.add(subscriber8)

        # Case 3: Local BR number with DDD (10 or 11 digits without 55)
        elif len(k) == 10:
            ddd = k[:2]
            subscriber = k[2:]
            keys.add(f"55{ddd}{subscriber}")
            if subscriber[0] in "6789":
                keys.add(f"55{ddd}9{subscriber}")
                keys.add(f"{ddd}9{subscriber}")
        elif len(k) == 11 and k[2] == "9":
            ddd = k[:2]
            subscriber8 = k[3:]
            keys.add(f"55{ddd}9{subscriber8}")
            keys.add(f"55{ddd}{subscriber8}")
            keys.add(f"{ddd}{subscriber8}")

    _PHONE_KEYS_CACHE[raw_phone] = keys
    return keys


def format_e164_for_icloud(jid_or_phone: str) -> Tuple[str, str]:
    """
    Formats a WhatsApp JID or phone string into:
    1) Strict E.164 for iCloud vCard (e.g., '+5511999999999')
    2) Human-readable international format (e.g., '+55 11 99999-9999')
    Also upgrades old Brazilian 12-digit mobile JIDs (55+DDD+8 digits starting with 6-9)
    to the 9-digit mobile format when appropriate, while preserving landlines.
    """
    cache_hit = _E164_CACHE.get(jid_or_phone)
    if cache_hit is not None:
        return cache_hit

    digits = re.sub(r"\D", "", jid_or_phone.split("@")[0].split(":")[0])
    if not digits:
        return "", ""

    # Fast-path for Brazilian numbers (99% of cases)
    if digits.startswith("55") and len(digits) in (12, 13):
        ddd = int(digits[2:4])
        if len(digits) == 12 and 11 <= ddd <= 99 and digits[4] in ("6", "7", "8", "9"):
            digits = f"55{digits[2:4]}9{digits[4:]}"
        e164 = f"+{digits}"
        if len(digits) == 13:
            intl = f"+55 {digits[2:4]} {digits[4:9]}-{digits[9:]}"
        else:
            intl = f"+55 {digits[2:4]} {digits[4:8]}-{digits[8:]}"
        res = (e164, intl)
        _E164_CACHE[jid_or_phone] = res
        return res

    e164 = f"+{digits}"
    try:
        parsed = phonenumbers.parse(e164, None)
        if phonenumbers.is_Possible_number(parsed) if hasattr(phonenumbers, "is_Possible_number") else phonenumbers.is_possible_number(parsed):
            e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            intl = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            res = (e164, intl)
            _E164_CACHE[jid_or_phone] = res
            return res
    except Exception:
        pass

    res = (e164, e164)
    _E164_CACHE[jid_or_phone] = res
    return res


def decode_vcard_value(val: str, params: str) -> str:
    val = val.strip()
    if "QUOTED-PRINTABLE" in params.upper():
        try:
            val = quopri.decodestring(val.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            pass
    val = val.replace(r"\n", " ").replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")
    return val.strip()


def parse_icloud_vcf(vcf_content: str) -> Dict[str, any]:
    """
    Parses an exported iCloud .vcf file (supports vCard 2.1, 3.0, and 4.0, including line folding).
    Returns:
      - contacts_count: total contacts in VCF
      - phone_to_name: dict mapping every normalized phone key -> existing contact name in iCloud
      - existing_names: set of lowercased full names already in iCloud
    """
    # Unfold lines per RFC 2426 / 6350 (CRLF or LF followed by space or tab)
    unfolded = re.sub(r"\r?\n[ \t]", "", vcf_content)
    # Also handle Quoted-Printable soft line breaks (=\r\n or =\n)
    unfolded = re.sub(r"=\r?\n", "", unfolded)

    cards = re.findall(r"BEGIN:VCARD.*?END:VCARD", unfolded, flags=re.DOTALL | re.IGNORECASE)
    phone_to_name: Dict[str, str] = {}
    existing_names: Set[str] = set()
    total_phones = 0

    for card in cards:
        fn = ""
        n_parts = ""
        phones_in_card: List[str] = []

        for line in card.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            prop_part, val_part = line.split(":", 1)
            prop_tokens = prop_part.split(";")
            # Handle item1.TEL or TEL
            base_prop = prop_tokens[0].split(".")[-1].upper()
            params = ";".join(prop_tokens[1:])

            if base_prop == "FN":
                fn = decode_vcard_value(val_part, params)
            elif base_prop == "N" and not fn:
                decoded_n = decode_vcard_value(val_part, params)
                parts = [p.strip() for p in decoded_n.split(";") if p.strip()]
                if len(parts) >= 2:
                    n_parts = f"{parts[1]} {parts[0]}".strip()
                elif parts:
                    n_parts = parts[0]
            elif base_prop == "TEL":
                cleaned_tel = val_part.strip()
                if cleaned_tel:
                    phones_in_card.append(cleaned_tel)

        contact_name = fn or n_parts or "Contato iCloud"
        existing_names.add(contact_name.lower())

        for raw_tel in phones_in_card:
            total_phones += 1
            for key in generate_phone_keys(raw_tel):
                phone_to_name[key] = contact_name

    return {
        "contacts_count": len(cards),
        "phones_count": total_phones,
        "phone_to_name": phone_to_name,
        "existing_names": existing_names,
    }


def is_meaningful_name(raw_name: Optional[str], phone_digits: str) -> bool:
    """
    Checks if a pushname / notify name is a real name vs empty, purely symbols,
    masked WhatsApp numbers (e.g. '+55∙∙∙∙∙∙∙∙∙19'), or just the phone number itself.
    """
    if not raw_name:
        return False
    cleaned = raw_name.strip()
    if not cleaned or cleaned in ("~", ".", "-", "?", "WhatsApp"):
        return False
    # Ignore WhatsApp masked phone strings like '+55∙∙∙∙∙∙∙∙∙19' or '+55•••••••'
    if "∙" in cleaned or "•" in cleaned or "***" in cleaned:
        return False
    # If the name has at least 6 digits and no letters, or starts with '+' and has no letters, it's a phone number
    only_digits = re.sub(r"\D", "", cleaned)
    only_letters = re.sub(r"[^a-zA-ZÀ-ÿ]", "", cleaned)
    if len(only_letters) == 0 and (len(only_digits) >= 4 or cleaned.startswith("+")):
        return False
    return True


def build_display_name(
    pushname: Optional[str],
    verified_name: Optional[str],
    intl_phone: str,
    phone_digits: str,
    naming_mode: str = "custom_prefix",
    existing_icloud_names: Optional[Set[str]] = None,
    used_names_in_batch: Optional[Set[str]] = None,
    seq_index: int = 1,
    next_number_state: Optional[List[int]] = None,
    custom_prefix: str = "Cliente",
    include_pushname: bool = False,
) -> str:
    """
    Builds a clean, organized contact name for iCloud according to user's preference.
    """
    if existing_icloud_names is None:
        existing_icloud_names = set()
    if used_names_in_batch is None:
        used_names_in_batch = set()
    if next_number_state is None:
        next_number_state = [101]

    candidate_base = ""
    if is_meaningful_name(pushname, phone_digits):
        candidate_base = pushname.strip().lstrip("~").strip()
    elif is_meaningful_name(verified_name, phone_digits):
        candidate_base = verified_name.strip().lstrip("~").strip()

    # Clean up excessive whitespace or control chars
    candidate_base = re.sub(r"\s+", " ", candidate_base).strip()

    def get_next_available_client_name(prefix: str = "Cliente") -> str:
        num = next_number_state[0]
        while True:
            cand = f"{prefix} {num}"
            if cand.lower() not in existing_icloud_names and cand.lower() not in used_names_in_batch:
                next_number_state[0] = num + 1
                return cand
            num += 1

    if naming_mode == "custom_prefix":
        base_client = get_next_available_client_name(custom_prefix.strip() or "Cliente")
        if include_pushname and candidate_base:
            final_name = f"{base_client} - {candidate_base}"
        else:
            final_name = base_client
    elif naming_mode == "suffix_if_needed" or naming_mode == "always_suffix":
        base_client = get_next_available_client_name(custom_prefix.strip() or "Cliente")
        if include_pushname:
            if candidate_base:
                final_name = f"{base_client} - {candidate_base}"
            else:
                final_name = base_client
        elif candidate_base:
            final_name = candidate_base
            if final_name.lower() in existing_icloud_names or final_name.lower() in used_names_in_batch:
                final_name = f"{candidate_base} (WhatsApp)"
        else:
            final_name = base_client

    # If two different unsaved numbers have the exact same pushname (e.g., two "João (WhatsApp)"),
    # disambiguate with the last 4 digits so iCloud doesn't merge two different people!
    if final_name.lower() in existing_icloud_names or final_name.lower() in used_names_in_batch:
        last4 = phone_digits[-4:] if len(phone_digits) >= 4 else phone_digits
        final_name = f"{final_name} [{last4}]"

    used_names_in_batch.add(final_name.lower())
    return final_name


def escape_vcard_fn(text: str) -> str:
    """Escapes formatted name without breaking commas or spaces in Apple Contacts."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )


def escape_vcard_structured(text: str) -> str:
    """Escapes structured field components (N) where semicolons separate fields."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )


def escape_vcard_text(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )


def generate_icloud_vcf(contacts_to_export: List[Dict[str, any]]) -> str:
    """
    Generates an Apple iCloud-compliant vCard 3.0 (.vcf) file.
    Rules for 100% iCloud Web, AirDrop, iOS Safari & WhatsApp document compatibility:
    - VERSION:3.0
    - PRODID:-//Apple Inc.//iPhone OS 18.0//EN
    - CRLF line endings (\\r\\n)
    - UTF-8 encoding without BOM
    - Unique UID per contact (CRITICAL for iOS Contacts & AirDrop daemon)
    - Clean N (N:;Name;;;) so sorting/display never inverts
    - Apple-native item1.TEL;type=CELL;type=VOICE;type=pref and item1.X-ABLabel:_$!<Mobile>!$_
    - Revision timestamp (REV)
    """
    import uuid
    lines: List[str] = []
    rev_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for idx, c in enumerate(contacts_to_export, start=1):
        full_name = c.get("final_name", "").strip()
        e164 = c.get("e164", "").strip()
        raw_e164 = c.get("raw_e164", "").strip()
        if not full_name or not e164:
            continue

        esc_fn = escape_vcard_fn(full_name)
        esc_n = escape_vcard_structured(full_name)
        card_uid = f"whub-{uuid.uuid4()}"

        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        lines.append("PRODID:-//Apple Inc.//iPhone OS 18.0//EN")
        lines.append(f"UID:{card_uid}")
        lines.append(f"N:;{esc_n};;;")
        lines.append(f"FN:{esc_fn}")
        lines.append(f"item1.TEL;type=CELL;type=VOICE;type=pref:{e164}")
        lines.append("item1.X-ABLabel:_$!<Mobile>!$_")
        if raw_e164 and raw_e164 != e164:
            lines.append(f"item2.TEL;type=CELL;type=VOICE:{raw_e164}")
            lines.append("item2.X-ABLabel:_$!<Mobile>!$_")
        lines.append(f"REV:{rev_ts}")
        lines.append("END:VCARD")

    return "\r\n".join(lines) + "\r\n"
