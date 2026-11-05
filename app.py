import os
import json
import time
import subprocess
import datetime
from typing import Dict, List, Any, Set
import requests
from flask import Flask, request, jsonify, Response, send_from_directory

from contact_engine import (
    generate_phone_keys,
    format_e164_for_icloud,
    parse_icloud_vcf,
    is_meaningful_name,
    build_display_name,
    generate_icloud_vcf,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ICLOUD_VCF_PATH = os.path.join(DATA_DIR, "icloud_existing.vcf")
EXPORTED_HISTORY_PATH = os.path.join(DATA_DIR, "exported_history.json")
CUSTOM_OVERRIDES_PATH = os.path.join(DATA_DIR, "custom_overrides.json")
BRIDGE_URL = "http://127.0.0.1:3847"

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB max upload limit

bridge_process = None


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def atomic_write_json(filepath: str, data: Any):
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filepath)


def ensure_bridge_running():
    global bridge_process
    try:
        r = requests.get(f"{BRIDGE_URL}/status", timeout=1.0)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    if bridge_process is None or bridge_process.poll() is not None:
        node_bin = os.path.join(BASE_DIR, ".runtime", "node", "bin", "node")
        if not os.path.exists(node_bin):
            node_bin = "node"
        bridge_script = os.path.join(BASE_DIR, "bridge.js")
        log_file = open(os.path.join(DATA_DIR, "bridge.log"), "a", encoding="utf-8")
        bridge_process = subprocess.Popen(
            [node_bin, bridge_script],
            cwd=BASE_DIR,
            stdout=log_file,
            stderr=log_file,
        )
        for _ in range(15):
            time.sleep(0.3)
            try:
                r = requests.get(f"{BRIDGE_URL}/status", timeout=0.5)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
    return False


def load_overrides() -> Dict[str, Any]:
    if os.path.exists(CUSTOM_OVERRIDES_PATH):
        try:
            with open(CUSTOM_OVERRIDES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "names": {},
        "excluded": [],
        "settings": {
            "naming_mode": "custom_prefix",
            "custom_prefix": "Cliente",
            "start_number": 101,
            "include_pushname": False,
        },
    }


def save_overrides(data: Dict[str, Any]):
    atomic_write_json(CUSTOM_OVERRIDES_PATH, data)


def load_exported_history() -> Dict[str, str]:
    """
    Returns a map of phone_key -> exported_name from explicit export history
    (data/exported_history.json) so that already-exported numbers are recognized.
    """
    exported_map: Dict[str, str] = {}
    if os.path.exists(EXPORTED_HISTORY_PATH):
        try:
            with open(EXPORTED_HISTORY_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        for pk in generate_phone_keys(k):
                            exported_map[pk] = v
        except Exception:
            pass

    return exported_map


def record_exported_contacts(contacts_list: List[Dict[str, Any]]):
    existing = {}
    if os.path.exists(EXPORTED_HISTORY_PATH):
        try:
            with open(EXPORTED_HISTORY_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    for c in contacts_list:
        e164 = c.get("e164") or c.get("phone_digits")
        name = c.get("final_name", "Contato Exportado")
        if e164:
            existing[e164] = name

    atomic_write_json(EXPORTED_HISTORY_PATH, existing)


def get_icloud_reference() -> Dict[str, Any]:
    if not os.path.exists(ICLOUD_VCF_PATH):
        return {
            "loaded": False,
            "contacts_count": 0,
            "phones_count": 0,
            "phone_to_name": {},
            "existing_names": set(),
            "filename": None,
        }
    try:
        with open(ICLOUD_VCF_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        parsed = parse_icloud_vcf(content)
        parsed["loaded"] = True
        parsed["filename"] = "icloud_existing.vcf"
        return parsed
    except Exception as e:
        return {
            "loaded": False,
            "error": str(e),
            "contacts_count": 0,
            "phones_count": 0,
            "phone_to_name": {},
            "existing_names": set(),
        }


def analyze_all_contacts(
    naming_mode: str = "custom_prefix",
    custom_prefix: str = "Cliente",
    start_number: int = 101,
    include_pushname: bool = False,
    only_with_chat_history: bool = True,
) -> Dict[str, Any]:
    ensure_bridge_running()
    bridge_user = None
    bridge_state = "disconnected"
    try:
        st_resp = requests.get(f"{BRIDGE_URL}/status", timeout=2.0)
        if st_resp.status_code == 200:
            st_data = st_resp.json()
            bridge_state = st_data.get("state", "disconnected")
            bridge_user = st_data.get("user")
    except Exception:
        pass

    # If WhatsApp is not connected, return 100% clean/empty state!
    if bridge_state != "connected" or not bridge_user:
        return {
            "summary": {
                "total_contacts_in_wa": 0,
                "total_1on1_analyzed": 0,
                "total_chats_1on1": 0,
                "new_unsaved_count": 0,
                "already_saved_count": 0,
                "ready_to_export_count": 0,
            },
            "new_contacts": [],
            "already_saved_contacts": [],
            "naming_mode": naming_mode,
            "custom_prefix": custom_prefix,
            "start_number": start_number,
        }

    # Detect user account change in Flask
    current_phone = bridge_user.get("id", "").split(":")[0].split("@")[0].strip() if bridge_user else ""
    user_tracker_file = os.path.join(DATA_DIR, "last_connected_user.txt")
    if current_phone:
        last_phone = ""
        if os.path.exists(user_tracker_file):
            try:
                with open(user_tracker_file, "r", encoding="utf-8") as f:
                    last_phone = f.read().strip()
            except Exception:
                pass
        if last_phone and last_phone != current_phone:
            # WhatsApp account changed! Wipe previous account's export history and custom edits
            for p in (EXPORTED_HISTORY_PATH, CUSTOM_OVERRIDES_PATH):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        try:
            with open(user_tracker_file, "w", encoding="utf-8") as f:
                f.write(current_phone)
        except Exception:
            pass

    wa_store = None
    try:
        raw_resp = requests.get(f"{BRIDGE_URL}/data", timeout=5.0)
        if raw_resp.status_code == 200:
            wa_store = raw_resp.json()
    except Exception:
        pass

    if not wa_store:
        wa_store = {"contacts": {}, "chats": {}, "syncHistoryBatches": 0, "totalMessagesScanned": 0}

    icloud_ref = get_icloud_reference()
    icloud_phone_map: Dict[str, str] = icloud_ref.get("phone_to_name", {})
    existing_icloud_names: Set[str] = set(icloud_ref.get("existing_names", set()))

    exported_phone_map: Dict[str, str] = load_exported_history()

    overrides = load_overrides()
    custom_names: Dict[str, str] = overrides.get("names", {})
    excluded_jids: Set[str] = set(overrides.get("excluded", []))

    wa_contacts: Dict[str, Any] = wa_store.get("contacts", {})
    wa_chats: Dict[str, Any] = wa_store.get("chats", {})
    wa_lid_meta: Dict[str, Any] = wa_store.get("lidMeta", {})
    wa_pn_to_lid: Dict[str, str] = wa_store.get("pnToLid", {})

    # Build a normalized phone-key map for ALL saved WhatsApp AddressBook contacts!
    # This guarantees that even if a contact is saved in the iPhone agenda with 9 digits
    # (e.g., 5541998644497) while the WhatsApp chat JID has 8 digits (554198644497),
    # or vice-versa, it is 100% recognized as ALREADY SAVED and never duplicated!
    wa_saved_phone_map: Dict[str, str] = {}
    wa_pushname_phone_map: Dict[str, str] = {}

    for c_jid, c_obj in wa_contacts.items():
        if not c_jid.endswith("@s.whatsapp.net"):
            continue
        c_digits = c_jid.split("@")[0].split(":")[0]
        if not c_digits or len(c_digits) < 7:
            continue

        saved_n = (c_obj.get("name") or "").strip()
        is_ab = bool(c_obj.get("isAddressBook"))
        notify_n = (c_obj.get("notify") or "").strip()

        if saved_n.startswith("~") and not is_ab:
            cleaned_t = saved_n.lstrip("~").strip()
            if cleaned_t and not notify_n:
                notify_n = cleaned_t
            saved_n = ""

        if saved_n and ("∙" in saved_n or "•" in saved_n):
            saved_n = ""
        if notify_n and ("∙" in notify_n or "•" in notify_n):
            notify_n = ""

        p_keys = generate_phone_keys(c_digits)
        if saved_n or is_ab:
            effective_saved = saved_n or notify_n or f"+{c_digits}"
            existing_icloud_names.add(effective_saved.lower())
            # Also register the prefix-number part before " - " (e.g., "Mundo Apple 157" from "Mundo Apple 157 - Guilherme Yuki")
            # so we never reuse number 157 when generating new numbered contacts!
            if " - " in effective_saved:
                existing_icloud_names.add(effective_saved.split(" - ")[0].strip().lower())
            for pk in p_keys:
                wa_saved_phone_map[pk] = effective_saved

        if notify_n and is_meaningful_name(notify_n, c_digits):
            for pk in p_keys:
                if pk not in wa_pushname_phone_map:
                    wa_pushname_phone_map[pk] = notify_n

    for exp_n in exported_phone_map.values():
        if exp_n:
            existing_icloud_names.add(exp_n.lower())
            if " - " in exp_n:
                existing_icloud_names.add(exp_n.split(" - ")[0].strip().lower())

    next_number_state = [max(1, int(start_number))]

    all_jids: Set[str] = set(wa_chats.keys()) | set(wa_contacts.keys())

    self_jid_prefix = None
    if wa_store.get("user") and wa_store["user"].get("id"):
        self_jid_prefix = wa_store["user"]["id"].split(":")[0].split("@")[0]

    seen_canonical_phones: Set[str] = set()
    used_names_in_batch: Set[str] = set()

    new_contacts: List[Dict[str, Any]] = []
    already_saved_contacts: List[Dict[str, Any]] = []

    def sort_key(jid: str):
        ch = wa_chats.get(jid, {})
        ts = ch.get("conversationTimestamp") or ch.get("firstSeenTimestamp") or 0
        return (0 if jid in wa_chats else 1, ts, jid)

    sorted_jids = sorted(all_jids, key=sort_key)

    for jid in sorted_jids:
        if not jid.endswith("@s.whatsapp.net"):
            continue

        phone_digits = jid.split("@")[0].split(":")[0]
        if not phone_digits or len(phone_digits) < 8:
            continue
        if self_jid_prefix and phone_digits == self_jid_prefix:
            continue

        in_chats = jid in wa_chats
        c_info = wa_contacts.get(jid, {})
        ch_info = wa_chats.get(jid, {})

        phone_keys = generate_phone_keys(phone_digits)
        if any(k in seen_canonical_phones for k in phone_keys):
            continue

        e164, intl_phone = format_e164_for_icloud(phone_digits)
        raw_e164 = f"+{phone_digits}"

        # Check 1: Is it in the uploaded iCloud .vcf?
        matched_icloud_name = None
        for k in phone_keys:
            if k in icloud_phone_map:
                matched_icloud_name = icloud_phone_map[k]
                break

        # Check 2: Is it saved in the phone's WhatsApp Address Book (including 8/9-digit variations)?
        wa_addressbook_name = None
        for k in phone_keys:
            if k in wa_saved_phone_map:
                wa_addressbook_name = wa_saved_phone_map[k]
                break

        # Resolve best available pushname across contacts, lidMeta, pnToLid, and chat metadata
        pushname = (
            c_info.get("notify")
            or c_info.get("displayName")
            or c_info.get("name")
            or c_info.get("shortName")
            or ""
        )
        if "∙" in pushname or "•" in pushname:
            pushname = ""
        if pushname:
            pushname = pushname.strip().lstrip("~").strip()
            if not is_meaningful_name(pushname, phone_digits):
                pushname = ""

        if not pushname:
            lid_candidate = (c_info.get("lid") or "").split("@")[0] or wa_pn_to_lid.get(phone_digits, "")
            if not lid_candidate:
                for k in phone_keys:
                    if k in wa_pn_to_lid:
                        lid_candidate = wa_pn_to_lid[k]
                        break
            if lid_candidate and lid_candidate in wa_lid_meta:
                lm = wa_lid_meta[lid_candidate]
                lm_cand = (lm.get("notify") or lm.get("displayName") or lm.get("name") or "").strip().lstrip("~").strip()
                if lm_cand and is_meaningful_name(lm_cand, phone_digits):
                    pushname = lm_cand

        if not pushname:
            for k in phone_keys:
                if k in wa_pushname_phone_map:
                    pushname = wa_pushname_phone_map[k]
                    break

        if not pushname:
            ch_cand = (ch_info.get("displayName") or ch_info.get("name") or ch_info.get("username") or "").strip().lstrip("~").strip()
            if ch_cand and ch_cand != wa_addressbook_name and is_meaningful_name(ch_cand, phone_digits):
                pushname = ch_cand

        verified_name = (c_info.get("verifiedName") or "").strip()
        if not verified_name:
            lid_candidate = (c_info.get("lid") or "").split("@")[0] or wa_pn_to_lid.get(phone_digits, "")
            if lid_candidate and lid_candidate in wa_lid_meta:
                verified_name = (wa_lid_meta[lid_candidate].get("verifiedName") or "").strip()

        # Check 3: Was it already exported in a previous VCF batch?
        matched_exported_name = None
        if not matched_icloud_name and not wa_addressbook_name:
            for k in phone_keys:
                if k in exported_phone_map:
                    matched_exported_name = exported_phone_map[k]
                    break

        is_saved_icloud = matched_icloud_name is not None
        is_saved_wa = bool(wa_addressbook_name and wa_addressbook_name.strip())
        is_saved_exported = matched_exported_name is not None

        # HARD GROUP BLOCK: An unsaved number MUST be an actual 1-on-1 private chat (`in_chats == True`).
        # Group members (even those with pushnames in PUSH_NAME sync) are NEVER allowed into `new_contacts`!
        if not (is_saved_icloud or is_saved_wa or is_saved_exported):
            if not in_chats:
                continue

        for k in phone_keys:
            seen_canonical_phones.add(k)

        conv_ts = ch_info.get("conversationTimestamp") or 0
        first_ts = ch_info.get("firstSeenTimestamp") or conv_ts
        last_date_str = (
            datetime.datetime.fromtimestamp(conv_ts).strftime("%d/%m/%Y %H:%M")
            if conv_ts > 0
            else "Sincronizado"
        )

        if is_saved_icloud or is_saved_wa or is_saved_exported:
            if is_saved_icloud:
                source_label = "iCloud (.vcf)"
            elif is_saved_wa:
                source_label = "Salvo na Agenda / iCloud"
            else:
                source_label = "Já Exportado (.vcf)"

            already_saved_contacts.append(
                {
                    "jid": jid,
                    "phone_digits": phone_digits,
                    "e164": e164,
                    "raw_e164": raw_e164,
                    "intl_phone": intl_phone,
                    "saved_name": matched_icloud_name or wa_addressbook_name or matched_exported_name,
                    "pushname": pushname or "",
                    "source": source_label,
                    "in_chats": in_chats,
                    "last_interaction": last_date_str,
                    "timestamp": conv_ts,
                }
            )
        else:
            auto_name = build_display_name(
                pushname=pushname,
                verified_name=verified_name,
                intl_phone=intl_phone,
                phone_digits=phone_digits,
                naming_mode=naming_mode,
                existing_icloud_names=existing_icloud_names,
                used_names_in_batch=used_names_in_batch,
                seq_index=len(new_contacts) + 1,
                next_number_state=next_number_state,
                custom_prefix=custom_prefix,
                include_pushname=include_pushname,
            )

            final_name = custom_names.get(jid, auto_name)
            has_custom_override = jid in custom_names
            is_excluded = jid in excluded_jids

            new_contacts.append(
                {
                    "jid": jid,
                    "phone_digits": phone_digits,
                    "e164": e164,
                    "raw_e164": raw_e164,
                    "intl_phone": intl_phone,
                    "pushname": pushname or "",
                    "verified_name": verified_name or "",
                    "auto_name": auto_name,
                    "final_name": final_name,
                    "has_custom_override": has_custom_override,
                    "has_pushname": bool(pushname and pushname.strip()),
                    "in_chats": in_chats,
                    "message_count": ch_info.get("messageCount", 0),
                    "last_interaction": last_date_str,
                    "first_timestamp": first_ts,
                    "timestamp": conv_ts,
                    "excluded": is_excluded,
                }
            )

    saved_in_chats_count = sum(1 for c in already_saved_contacts if c.get("in_chats"))

    return {
        "icloud_reference": {
            "loaded": icloud_ref.get("loaded", False),
            "contacts_count": icloud_ref.get("contacts_count", 0),
            "phones_count": icloud_ref.get("phones_count", 0),
        },
        "summary": {
            "total_1on1_analyzed": len(new_contacts) + len(already_saved_contacts),
            "total_chats_1on1": len(new_contacts) + saved_in_chats_count,
            "new_unsaved_count": len(new_contacts),
            "ready_to_export_count": sum(1 for c in new_contacts if not c["excluded"]),
            "already_saved_count": len(already_saved_contacts),
            "saved_in_chats_count": saved_in_chats_count,
            "saved_in_icloud_vcf": sum(1 for c in already_saved_contacts if c["source"] == "iCloud (.vcf)"),
            "saved_in_wa_addressbook": sum(
                1 for c in already_saved_contacts if c["source"] != "iCloud (.vcf)"
            ),
        },
        "new_contacts": new_contacts,
        "already_saved_contacts": already_saved_contacts,
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    ensure_bridge_running()
    try:
        r = requests.get(f"{BRIDGE_URL}/status", timeout=3.0)
        bridge_st = r.json()
    except Exception as e:
        bridge_st = {
            "state": "disconnected",
            "lastMessage": f"Iniciando serviço do WhatsApp... ({e})",
            "stats": {},
        }

    icloud_ref = get_icloud_reference()
    return jsonify(
        {
            "whatsapp": bridge_st,
            "icloud": {
                "loaded": icloud_ref.get("loaded", False),
                "contacts_count": icloud_ref.get("contacts_count", 0),
                "phones_count": icloud_ref.get("phones_count", 0),
            },
        }
    )


@app.route("/api/contacts", methods=["GET"])
def api_contacts():
    naming_mode = request.args.get("naming_mode", "custom_prefix")
    custom_prefix = request.args.get("custom_prefix", "Cliente").strip() or "Cliente"
    try:
        start_number = int(request.args.get("start_number", "101"))
    except ValueError:
        start_number = 101
    include_pushname = request.args.get("include_pushname", "false").lower() == "true"
    only_chats = request.args.get("only_chats", "true").lower() == "true"

    result = analyze_all_contacts(
        naming_mode=naming_mode,
        custom_prefix=custom_prefix,
        start_number=start_number,
        include_pushname=include_pushname,
        only_with_chat_history=only_chats,
    )
    return jsonify(result)


@app.route("/api/reset-exported-history", methods=["POST"])
def api_reset_exported_history():
    """Allows the user to clear local exported history if they want to re-export previously exported contacts."""
    if os.path.exists(EXPORTED_HISTORY_PATH):
        os.remove(EXPORTED_HISTORY_PATH)
    if os.path.exists(CUSTOM_OVERRIDES_PATH):
        os.remove(CUSTOM_OVERRIDES_PATH)
    return jsonify({"ok": True})


@app.route("/api/upload-icloud-vcf", methods=["POST"])
def api_upload_icloud_vcf():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Nenhum arquivo enviado."}), 400
    f = request.files["file"]
    content = f.read().decode("utf-8", errors="replace")
    if "BEGIN:VCARD" not in content.upper():
        return jsonify({"ok": False, "error": "O arquivo enviado não parece ser um arquivo .vcf (vCard) válido."}), 400

    with open(ICLOUD_VCF_PATH, "w", encoding="utf-8") as out:
        out.write(content)

    parsed = parse_icloud_vcf(content)
    return jsonify(
        {
            "ok": True,
            "contacts_count": parsed["contacts_count"],
            "phones_count": parsed["phones_count"],
        }
    )


@app.route("/api/remove-icloud-vcf", methods=["POST"])
def api_remove_icloud_vcf():
    if os.path.exists(ICLOUD_VCF_PATH):
        os.remove(ICLOUD_VCF_PATH)
    return jsonify({"ok": True})


@app.route("/api/update-contact", methods=["POST"])
def api_update_contact():
    body = request.get_json(force=True)
    jid = body.get("jid")
    if not jid:
        return jsonify({"ok": False, "error": "JID ausente"}), 400

    overrides = load_overrides()
    if "final_name" in body:
        new_name = str(body["final_name"]).strip()[:120]
        if new_name:
            overrides["names"][jid] = new_name
        else:
            overrides["names"].pop(jid, None)

    if "excluded" in body:
        excl = set(overrides.get("excluded", []))
        if body["excluded"]:
            excl.add(jid)
        else:
            excl.discard(jid)
        overrides["excluded"] = list(excl)

    save_overrides(overrides)
    return jsonify({"ok": True})


@app.route("/api/export-vcf", methods=["GET", "POST"])
def api_export_vcf():
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        naming_mode = body.get("naming_mode", "custom_prefix")
        custom_prefix = (body.get("custom_prefix") or "Cliente").strip() or "Cliente"
        try:
            start_number = int(body.get("start_number", 101))
        except (ValueError, TypeError):
            start_number = 101
        include_pushname = bool(body.get("include_pushname", False))
        only_chats = bool(body.get("only_chats", True))
        client_contacts = body.get("contacts")
    else:
        naming_mode = request.args.get("naming_mode", "custom_prefix")
        custom_prefix = request.args.get("custom_prefix", "Cliente").strip() or "Cliente"
        try:
            start_number = int(request.args.get("start_number", "101"))
        except ValueError:
            start_number = 101
        include_pushname = request.args.get("include_pushname", "false").lower() == "true"
        only_chats = request.args.get("only_chats", "true").lower() == "true"
        client_contacts = None

    if isinstance(client_contacts, list) and len(client_contacts) > 0:
        contacts_to_export = [c for c in client_contacts if not c.get("excluded") and c.get("final_name") and c.get("e164")]
    else:
        analysis = analyze_all_contacts(
            naming_mode=naming_mode,
            custom_prefix=custom_prefix,
            start_number=start_number,
            include_pushname=include_pushname,
            only_with_chat_history=only_chats,
        )
        contacts_to_export = [c for c in analysis["new_contacts"] if not c.get("excluded")]

    vcf_str = generate_icloud_vcf(contacts_to_export)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"Contatos_Novos_WhatsApp_para_iCloud_{timestamp}.vcf"

    export_path = os.path.join(BASE_DIR, filename)
    with open(export_path, "wb") as f:
        f.write(vcf_str.encode("utf-8"))

    record_exported_contacts(contacts_to_export)

    return Response(
        vcf_str.encode("utf-8"),
        mimetype="text/vcard; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/whatsapp/logout", methods=["POST"])
def api_wa_logout():
    ensure_bridge_running()
    try:
        requests.post(f"{BRIDGE_URL}/logout", timeout=5.0)
    except Exception:
        pass
    # Clear all account data, raw store, exported history, custom overrides and exported vcf files
    for p in (
        os.path.join(DATA_DIR, "wa_raw_store.json"),
        os.path.join(DATA_DIR, "wa_raw_store.json.tmp"),
        EXPORTED_HISTORY_PATH,
        CUSTOM_OVERRIDES_PATH,
        os.path.join(DATA_DIR, "last_connected_user.txt"),
    ):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    for fname in os.listdir(BASE_DIR):
        if fname.startswith("Contatos_Novos_WhatsApp_para_iCloud_") and fname.endswith(".vcf"):
            try:
                os.remove(os.path.join(BASE_DIR, fname))
            except Exception:
                pass
    return jsonify({"ok": True})


@app.route("/api/whatsapp/reconnect", methods=["POST"])
def api_wa_reconnect():
    ensure_bridge_running()
    try:
        requests.post(f"{BRIDGE_URL}/reconnect", timeout=5.0)
    except Exception:
        pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    ensure_bridge_running()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5050"))
    print("=================================================================")
    print("🚀 WHUB - EXTRATOR INTELIGENTE WHATSAPP -> iCLOUD INICIADO!")
    print(f"👉 Acesse no seu navegador: http://{host}:{port}")
    print("=================================================================")
    app.run(host=host, port=port, debug=False)
