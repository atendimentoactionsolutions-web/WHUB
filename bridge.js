const pino = require("pino");
const QRCode = require("qrcode");
const fs = require("fs");
const path = require("path");
const http = require("http");

const AUTH_DIR = path.join(__dirname, "data", "wa_auth");
const RAW_DATA_FILE = path.join(__dirname, "data", "wa_raw_store.json");

fs.mkdirSync(path.join(__dirname, "data"), { recursive: true });

// In-memory store persisted to disk
let store = {
  contacts: {},     // pnJid -> { id, name (addressBook), isAddressBook, notify (pushName), verifiedName, lid }
  chats: {},        // pnJid -> { id, name, conversationTimestamp, firstSeenTimestamp, messageCount }
  lidToPn: {},      // lidNumber -> pnDigits
  pnToLid: {},      // pnDigits -> lidNumber
  lidMeta: {},      // lidNumber -> { notify, name, isAddressBook, isChat, conversationTimestamp, firstSeenTimestamp, messageCount }
  syncHistoryBatches: 0,
  totalMessagesScanned: 0,
  lastSyncAt: null,
};

function cleanDigits(val) {
  if (!val || typeof val !== "string") return "";
  return val.split("@")[0].split(":")[0].replace(/\D/g, "");
}

function isMaskedOrEmptyName(str) {
  if (!str || typeof str !== "string") return true;
  const s = str.trim();
  if (!s) return true;
  if (s.includes("∙") || s.includes("•")) return true;
  return false;
}

if (fs.existsSync(RAW_DATA_FILE)) {
  try {
    const saved = JSON.parse(fs.readFileSync(RAW_DATA_FILE, "utf-8"));
    store.chats = saved.chats || {};
    store.lidToPn = saved.lidToPn || {};
    store.pnToLid = saved.pnToLid || {};
    store.lidMeta = saved.lidMeta || {};
    store.syncHistoryBatches = saved.syncHistoryBatches || 0;
    store.totalMessagesScanned = saved.totalMessagesScanned || 0;
    store.lastSyncAt = saved.lastSyncAt || null;

    // Clean up any previously polluted contacts (only keep contacts that are in 1-on-1 chats, or saved in AddressBook)
    const rawContacts = saved.contacts || {};
    const cleanedContacts = {};
    for (const [jid, c] of Object.entries(rawContacts)) {
      if (store.chats[jid] || c.isAddressBook || (c.name && !isMaskedOrEmptyName(c.name))) {
        if (c.notify && isMaskedOrEmptyName(c.notify)) {
          delete c.notify;
        }
        const lidNum = (c.lid || "").split("@")[0];
        if (!c.notify && lidNum && store.lidMeta[lidNum] && store.lidMeta[lidNum].notify) {
          c.notify = store.lidMeta[lidNum].notify;
        }
        if (!c.verifiedName && lidNum && store.lidMeta[lidNum] && store.lidMeta[lidNum].verifiedName) {
          c.verifiedName = store.lidMeta[lidNum].verifiedName;
        }
        cleanedContacts[jid] = c;
      }
    }
    store.contacts = cleanedContacts;
  } catch (e) {
    console.error("Failed to read existing store:", e.message);
  }
}

let hasScannedAuthDirOnce = false;
function loadLidMappingsFromAuthDir(force = false) {
  if (!fs.existsSync(AUTH_DIR)) return;

  // Always check every LID in store.lidMeta to ensure its reverse PN mapping is loaded!
  for (const [lid, meta] of Object.entries(store.lidMeta)) {
    let pn = store.lidToPn[lid];
    if (!pn) {
      const revFile = path.join(AUTH_DIR, `lid-mapping-${lid}_reverse.json`);
      if (fs.existsSync(revFile)) {
        try {
          const pnNum = JSON.parse(fs.readFileSync(revFile, "utf-8"));
          if (pnNum) pn = String(pnNum);
        } catch (e) {}
      }
    }
    if (pn) {
      registerLidPnMapping(lid, pn);
    }
  }

  if (hasScannedAuthDirOnce && !force) return;
  hasScannedAuthDirOnce = true;

  try {
    const files = fs.readdirSync(AUTH_DIR);
    for (const file of files) {
      if (file.startsWith("lid-mapping-") && file.endsWith("_reverse.json")) {
        const lidNum = file.replace("lid-mapping-", "").replace("_reverse.json", "");
        if (!store.lidToPn[lidNum]) {
          const fullPath = path.join(AUTH_DIR, file);
          try {
            const pnNum = JSON.parse(fs.readFileSync(fullPath, "utf-8"));
            if (lidNum && pnNum) {
              registerLidPnMapping(lidNum, String(pnNum));
            }
          } catch (err) {}
        }
      }
    }
  } catch (e) {
    console.error("Error scanning lid-mapping files:", e.message);
  }
}

// Fast single-LID lookup on disk if not in memory yet
function lookupSingleLidOnDisk(lid) {
  if (!lid) return null;
  if (store.lidToPn[lid]) return store.lidToPn[lid];
  const revFile = path.join(AUTH_DIR, `lid-mapping-${lid}_reverse.json`);
  if (fs.existsSync(revFile)) {
    try {
      const pnNum = JSON.parse(fs.readFileSync(revFile, "utf-8"));
      if (pnNum) {
        const cleanPn = cleanDigits(String(pnNum));
        if (cleanPn.length >= 7) {
          store.lidToPn[lid] = cleanPn;
          store.pnToLid[cleanPn] = lid;
          return cleanPn;
        }
      }
    } catch (e) {}
  }
  return null;
}

function registerLidPnMapping(lidRaw, pnRaw) {
  const lid = cleanDigits(String(lidRaw));
  const pn = cleanDigits(String(pnRaw));
  if (!lid || !pn || pn.length < 7) return;

  store.lidToPn[lid] = pn;
  store.pnToLid[pn] = lid;

  const pnJid = `${pn}@s.whatsapp.net`;
  if (store.contacts[pnJid]) {
    store.contacts[pnJid].lid = `${lid}@lid`;
  }

  // Merge metadata ONLY if this LID belongs to an actual 1-on-1 private chat (`isChat`) or a saved AddressBook contact (`isAddressBook`).
  // Never promote PUSH_NAME-only entries because WhatsApp includes group members in PUSH_NAME!
  const meta = store.lidMeta[lid];
  if (meta) {
    const isRelevantContact =
      meta.isAddressBook ||
      meta.isChat ||
      meta.conversationTimestamp ||
      meta.messageCount ||
      Boolean(store.chats[pnJid]) ||
      Boolean(store.contacts[pnJid] && store.contacts[pnJid].isAddressBook);

    if (isRelevantContact) {
      if (!store.contacts[pnJid]) {
        store.contacts[pnJid] = { id: pnJid, lid: `${lid}@lid` };
      } else {
        store.contacts[pnJid].lid = `${lid}@lid`;
      }

      if (meta.name && meta.isAddressBook) {
        store.contacts[pnJid].name = meta.name;
        store.contacts[pnJid].isAddressBook = true;
      }
      if (meta.notify && !isMaskedOrEmptyName(meta.notify) && !store.contacts[pnJid].notify) {
        store.contacts[pnJid].notify = meta.notify;
      }
      if (meta.verifiedName && !store.contacts[pnJid].verifiedName) {
        store.contacts[pnJid].verifiedName = meta.verifiedName;
      }
    }

    if (meta.isChat || meta.conversationTimestamp || meta.messageCount) {
      if (!store.chats[pnJid]) {
        store.chats[pnJid] = { id: pnJid, messageCount: 0 };
      }
      if (meta.name && !isMaskedOrEmptyName(meta.name) && !store.chats[pnJid].name) {
        store.chats[pnJid].name = meta.name;
      }
      if ((meta.conversationTimestamp || 0) > (store.chats[pnJid].conversationTimestamp || 0)) {
        store.chats[pnJid].conversationTimestamp = meta.conversationTimestamp;
      }
      if (
        meta.firstSeenTimestamp &&
        (!store.chats[pnJid].firstSeenTimestamp || meta.firstSeenTimestamp < store.chats[pnJid].firstSeenTimestamp)
      ) {
        store.chats[pnJid].firstSeenTimestamp = meta.firstSeenTimestamp;
      }
      if ((meta.messageCount || 0) > (store.chats[pnJid].messageCount || 0)) {
        store.chats[pnJid].messageCount = meta.messageCount;
      }
    }
  }
}

// Resolve any JID (@s.whatsapp.net or @lid) or object with {id, phoneNumber, pnJid, lid, lidJid}
function resolveToPnJid(rawObjOrJid) {
  if (!rawObjOrJid) return { pnJid: null, lid: null };

  if (typeof rawObjOrJid === "string") {
    if (rawObjOrJid.endsWith("@s.whatsapp.net") || rawObjOrJid.endsWith("@hosted")) {
      const pn = cleanDigits(rawObjOrJid);
      if (pn.length >= 7) {
        return { pnJid: `${pn}@s.whatsapp.net`, lid: store.pnToLid[pn] || null };
      }
    }
    if (rawObjOrJid.endsWith("@lid") || rawObjOrJid.endsWith("@hosted.lid")) {
      const lid = cleanDigits(rawObjOrJid);
      const pn = lookupSingleLidOnDisk(lid);
      return { pnJid: pn ? `${pn}@s.whatsapp.net` : null, lid };
    }
    return { pnJid: null, lid: null };
  }

  // Object form
  const idStr = rawObjOrJid.id || "";
  const pnCandidate = rawObjOrJid.phoneNumber || rawObjOrJid.pnJid || (idStr.endsWith("@s.whatsapp.net") ? idStr : null);
  const lidCandidate =
    rawObjOrJid.lid || rawObjOrJid.lidJid || rawObjOrJid.accountLid || (idStr.endsWith("@lid") ? idStr : null);

  if (lidCandidate && pnCandidate) {
    registerLidPnMapping(lidCandidate, pnCandidate);
  }

  if (pnCandidate) {
    const pn = cleanDigits(pnCandidate);
    if (pn.length >= 7) {
      return { pnJid: `${pn}@s.whatsapp.net`, lid: lidCandidate ? cleanDigits(lidCandidate) : store.pnToLid[pn] || null };
    }
  }

  if (lidCandidate) {
    const lid = cleanDigits(lidCandidate);
    const mappedPn = lookupSingleLidOnDisk(lid);
    return { pnJid: mappedPn ? `${mappedPn}@s.whatsapp.net` : null, lid };
  }

  return { pnJid: null, lid: null };
}

let saveTimeout = null;
function scheduleSave() {
  if (saveTimeout) return;
  saveTimeout = setTimeout(() => {
    saveTimeout = null;
    try {
      const tmpFile = `${RAW_DATA_FILE}.tmp`;
      fs.writeFileSync(tmpFile, JSON.stringify(store, null, 2), "utf-8");
      fs.renameSync(tmpFile, RAW_DATA_FILE);
    } catch (e) {
      console.error("Error saving raw store:", e.message);
    }
  }, 300);
}

// Upsert contact from HistorySync (here `name` is often chat displayName/pushname, NOT necessarily address book)
function upsertHistoryContact(raw) {
  if (!raw || !raw.id) return;
  if (raw.id.endsWith("@g.us") || raw.id.endsWith("@broadcast") || raw.id.endsWith("@newsletter")) return;

  const { pnJid, lid } = resolveToPnJid(raw);
  const cleanNotify = raw.notify && !isMaskedOrEmptyName(raw.notify) ? raw.notify.trim().replace(/^~\s*/, "") : "";
  // Only use `raw.name` from HistorySync as a pushname fallback if `raw.notify` is present or if `raw.name` starts with `~`.
  // Otherwise `raw.name` in HistorySync comes from `chat.name` (the already-saved AddressBook name!).
  const cleanNameAsPush =
    raw.name && !isMaskedOrEmptyName(raw.name) && raw.name.trim().startsWith("~")
      ? raw.name.trim().replace(/^~\s*/, "")
      : "";
  const cleanVerified = raw.verifiedName && !isMaskedOrEmptyName(raw.verifiedName) ? raw.verifiedName.trim() : "";

  if (lid) {
    store.lidMeta[lid] = store.lidMeta[lid] || {};
    if (cleanNotify) store.lidMeta[lid].notify = cleanNotify;
    if (cleanNameAsPush && !store.lidMeta[lid].notify) store.lidMeta[lid].notify = cleanNameAsPush;
    if (cleanVerified) store.lidMeta[lid].verifiedName = cleanVerified;
  }

  if (!pnJid) return;
  const pnDigits = cleanDigits(pnJid);
  if (pnDigits && (cleanNotify || cleanNameAsPush || cleanVerified)) {
    store.pnMeta = store.pnMeta || {};
    store.pnMeta[pnDigits] = store.pnMeta[pnDigits] || {};
    if (cleanNotify) store.pnMeta[pnDigits].notify = cleanNotify;
    if (cleanNameAsPush && !store.pnMeta[pnDigits].notify) store.pnMeta[pnDigits].notify = cleanNameAsPush;
    if (cleanVerified) store.pnMeta[pnDigits].verifiedName = cleanVerified;
  }

  // HARD GROUP BLOCK: Only attach pushname/verifiedName to contacts that are ALREADY a 1-on-1 chat or in AddressBook!
  if (!store.chats[pnJid] && !(store.contacts[pnJid] && store.contacts[pnJid].isAddressBook)) {
    return;
  }
  const existing = store.contacts[pnJid] || { id: pnJid };
  if (lid) existing.lid = `${lid}@lid`;
  if (cleanNotify) existing.notify = cleanNotify;
  if (cleanNameAsPush && !existing.notify) existing.notify = cleanNameAsPush;
  if (cleanVerified) existing.verifiedName = cleanVerified;
  store.contacts[pnJid] = existing;
}

// Upsert contact from `contacts.upsert` (AppState contactAction = genuinely saved in phone's address book!)
function upsertAddressBookContact(raw) {
  if (!raw || !raw.id) return;
  if (raw.id.endsWith("@g.us") || raw.id.endsWith("@broadcast") || raw.id.endsWith("@newsletter")) return;

  const { pnJid, lid } = resolveToPnJid(raw);
  const cleanName = raw.name && !isMaskedOrEmptyName(raw.name) ? raw.name.trim() : "";
  const cleanNotify = raw.notify && !isMaskedOrEmptyName(raw.notify) ? raw.notify.trim() : "";
  const cleanVerified = raw.verifiedName && !isMaskedOrEmptyName(raw.verifiedName) ? raw.verifiedName.trim() : "";

  if (lid) {
    store.lidMeta[lid] = store.lidMeta[lid] || {};
    if (cleanName) {
      store.lidMeta[lid].name = cleanName;
      store.lidMeta[lid].isAddressBook = true;
    }
    if (cleanNotify) store.lidMeta[lid].notify = cleanNotify;
    if (cleanVerified) store.lidMeta[lid].verifiedName = cleanVerified;
  }

  if (!pnJid) return;
  const existing = store.contacts[pnJid] || { id: pnJid };
  if (lid) existing.lid = `${lid}@lid`;

  if (cleanName) {
    existing.name = cleanName;
    existing.isAddressBook = true;
  }
  if (cleanNotify) {
    existing.notify = cleanNotify;
  }
  if (cleanVerified) {
    existing.verifiedName = cleanVerified;
  }
  store.contacts[pnJid] = existing;
}

function upsertChat(raw) {
  if (!raw || !raw.id) return;
  // Ignore groups, broadcasts, newsletters
  if (raw.id.endsWith("@g.us") || raw.id.endsWith("@broadcast") || raw.id.endsWith("@newsletter")) return;

  const { pnJid, lid } = resolveToPnJid(raw);
  const ts = Number(raw.conversationTimestamp || raw.lastMsgTimestamp || 0);
  const rawAddressBookName = (raw.name || "").trim();
  const rawDisplayName = (raw.displayName || raw.username || "").trim();
  const abNameFromChat =
    !isMaskedOrEmptyName(rawAddressBookName) && !rawAddressBookName.startsWith("~") && !rawAddressBookName.startsWith("+")
      ? rawAddressBookName
      : "";
  const pushNameFromChat = !isMaskedOrEmptyName(rawDisplayName) ? rawDisplayName.replace(/^~\s*/, "").trim() : "";
  const chatName = pushNameFromChat || abNameFromChat;

  if (lid) {
    store.lidMeta[lid] = store.lidMeta[lid] || {};
    store.lidMeta[lid].isChat = true;
    if (abNameFromChat) {
      store.lidMeta[lid].name = abNameFromChat;
      store.lidMeta[lid].isAddressBook = true;
    }
    if (pushNameFromChat && !store.lidMeta[lid].notify) {
      store.lidMeta[lid].notify = pushNameFromChat;
    }
    if (ts > (store.lidMeta[lid].conversationTimestamp || 0)) {
      store.lidMeta[lid].conversationTimestamp = ts;
    }
    if (ts > 0 && (!store.lidMeta[lid].firstSeenTimestamp || ts < store.lidMeta[lid].firstSeenTimestamp)) {
      store.lidMeta[lid].firstSeenTimestamp = ts;
    }
  }

  if (!pnJid) return;

  const existing = store.chats[pnJid] || { id: pnJid, messageCount: 0 };
  if (chatName) existing.name = chatName;
  if (ts > (existing.conversationTimestamp || 0)) {
    existing.conversationTimestamp = ts;
  }
  if (ts > 0 && (!existing.firstSeenTimestamp || ts < existing.firstSeenTimestamp)) {
    existing.firstSeenTimestamp = ts;
  }
  store.chats[pnJid] = existing;

  if (!store.contacts[pnJid]) {
    store.contacts[pnJid] = { id: pnJid };
  }
  if (lid) {
    store.contacts[pnJid].lid = `${lid}@lid`;
  }
  if (abNameFromChat && !store.contacts[pnJid].name) {
    store.contacts[pnJid].name = abNameFromChat;
    store.contacts[pnJid].isAddressBook = true;
  }
  if (pushNameFromChat && !store.contacts[pnJid].notify) {
    store.contacts[pnJid].notify = pushNameFromChat;
  }
  if (lid && store.lidMeta[lid]) {
    if (store.lidMeta[lid].notify && !store.contacts[pnJid].notify) {
      store.contacts[pnJid].notify = store.lidMeta[lid].notify;
    }
    if (store.lidMeta[lid].verifiedName && !store.contacts[pnJid].verifiedName) {
      store.contacts[pnJid].verifiedName = store.lidMeta[lid].verifiedName;
    }
  }
  const pnDigits = cleanDigits(pnJid);
  if (pnDigits && store.pnMeta && store.pnMeta[pnDigits]) {
    if (store.pnMeta[pnDigits].notify && !store.contacts[pnJid].notify) {
      store.contacts[pnJid].notify = store.pnMeta[pnDigits].notify;
    }
    if (store.pnMeta[pnDigits].verifiedName && !store.contacts[pnJid].verifiedName) {
      store.contacts[pnJid].verifiedName = store.pnMeta[pnDigits].verifiedName;
    }
  }
}

function processMessage(msg) {
  if (!msg || !msg.key) return;
  const remoteJid = msg.key.remoteJid || "";
  if (remoteJid.endsWith("@g.us") || remoteJid.endsWith("@broadcast") || remoteJid.endsWith("@newsletter")) return;

  store.totalMessagesScanned += 1;

  const { pnJid, lid } = resolveToPnJid({
    id: remoteJid,
    pnJid: msg.key.senderPn || msg.participantPn || (remoteJid.endsWith("@s.whatsapp.net") ? remoteJid : undefined),
    lidJid: msg.key.senderLid || (remoteJid.endsWith("@lid") ? remoteJid : undefined),
  });

  const ts = Number(msg.messageTimestamp || 0);
  const rawPush = !msg.key.fromMe && msg.pushName ? msg.pushName.trim() : "";
  const pushName = isMaskedOrEmptyName(rawPush) ? "" : rawPush;

  if (lid) {
    store.lidMeta[lid] = store.lidMeta[lid] || {};
    store.lidMeta[lid].isChat = true;
    store.lidMeta[lid].messageCount = (store.lidMeta[lid].messageCount || 0) + 1;
    if (pushName) store.lidMeta[lid].notify = pushName;
    if (ts > 0) {
      if (!store.lidMeta[lid].conversationTimestamp || ts > store.lidMeta[lid].conversationTimestamp) {
        store.lidMeta[lid].conversationTimestamp = ts;
      }
      if (!store.lidMeta[lid].firstSeenTimestamp || ts < store.lidMeta[lid].firstSeenTimestamp) {
        store.lidMeta[lid].firstSeenTimestamp = ts;
      }
    }
  }

  if (!pnJid) return;

  if (!store.chats[pnJid]) {
    store.chats[pnJid] = { id: pnJid, messageCount: 0 };
  }
  store.chats[pnJid].messageCount = (store.chats[pnJid].messageCount || 0) + 1;

  if (ts > 0) {
    if (!store.chats[pnJid].conversationTimestamp || ts > store.chats[pnJid].conversationTimestamp) {
      store.chats[pnJid].conversationTimestamp = ts;
    }
    if (!store.chats[pnJid].firstSeenTimestamp || ts < store.chats[pnJid].firstSeenTimestamp) {
      store.chats[pnJid].firstSeenTimestamp = ts;
    }
  }

  if (!store.contacts[pnJid]) {
    store.contacts[pnJid] = { id: pnJid };
  }
  if (lid) {
    store.contacts[pnJid].lid = `${lid}@lid`;
  }
  if (pushName) {
    store.contacts[pnJid].notify = pushName;
  }
}

loadLidMappingsFromAuthDir();
scheduleSave();

let connectionStatus = {
  state: "disconnected",
  qrDataUrl: null,
  user: null,
  syncProgress: 0,
  lastMessage: "Aguardando inicialização...",
};

let sock = null;
let isStarting = false;

async function startWhatsApp() {
  if (isStarting) return;
  isStarting = true;

  try {
    const baileys = await import("@whiskeysockets/baileys");
    const makeWASocket = baileys.default?.default || baileys.default || baileys.makeWASocket;
    const {
      useMultiFileAuthState,
      DisconnectReason,
      fetchLatestBaileysVersion,
      Browsers,
    } = baileys;

    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion().catch(() => ({
      version: [2, 3000, 1043857760],
    }));

    connectionStatus.state = "connecting";
    connectionStatus.lastMessage = "Iniciando conexão com WhatsApp...";

    // IMPORTANT:
    // 1) Browsers.macOS("Desktop") tells WhatsApp's `getWebInfo` to set `WebSubPlatform.DARWIN`,
    //    which unlocks FULL multi-year HistorySync (`HistorySyncType.FULL` + `RECENT` + `INITIAL_BOOTSTRAP`)!
    // 2) shouldSyncHistoryMessage: () => true overrides Baileys' default which was dropping `HistorySyncType.FULL`!
    sock = makeWASocket({
      version,
      logger: pino({ level: "silent" }),
      auth: state,
      syncFullHistory: true,
      shouldSyncHistoryMessage: () => true,
      markOnlineOnConnect: false,
      generateHighQualityLinkPreview: false,
    });

    sock.ev.on("creds.update", () => {
      saveCreds();
    });

    sock.ev.on("lid-mapping.update", (mapping) => {
      if (Array.isArray(mapping)) {
        for (const m of mapping) {
          if (m && m.lid && m.pn) registerLidPnMapping(m.lid, m.pn);
        }
      } else if (mapping && mapping.lid && mapping.pn) {
        registerLidPnMapping(mapping.lid, mapping.pn);
      }
      scheduleSave();
    });

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        connectionStatus.state = "qr";
        connectionStatus.qrDataUrl = await QRCode.toDataURL(qr, { width: 320, margin: 2 });
        connectionStatus.lastMessage = "Escaneie o QR Code com o seu WhatsApp no iPhone";
      }

      if (connection === "open") {
        connectionStatus.state = "connected";
        connectionStatus.qrDataUrl = null;
        connectionStatus.user = sock.user;
        connectionStatus.lastMessage = `Conectado! Sincronizando histórico completo e agenda...`;
        scheduleSave();

        // Force AppState sync for all collections immediately (especially critical_unblock_low which holds saved AddressBook contacts!)
        setTimeout(async () => {
          try {
            if (sock && sock.resyncAppState) {
              // If we have fewer than 500 chats, reset regular_high and regular_low versions so Baileys emits all chats.update from snapshot!
              if (Object.keys(store.chats).length < 500) {
                for (const col of ["regular_high", "regular_low", "regular"]) {
                  const vf = path.join(AUTH_DIR, `app-state-sync-version-${col}.json`);
                  if (fs.existsSync(vf)) {
                    try { fs.unlinkSync(vf); } catch (e) {}
                  }
                }
              }
              await sock.resyncAppState(
                ["critical_block", "critical_unblock_low", "regular_high", "regular_low", "regular"],
                false
              );
              scheduleSave();
            }
          } catch (err) {
            console.error("AppState resync notice:", err.message);
          }
        }, 250);
      }

      if (connection === "close") {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const errMsg = lastDisconnect?.error?.message || "";
        console.error("Connection closed:", statusCode, errMsg);
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        connectionStatus.qrDataUrl = null;

        if (statusCode === DisconnectReason.loggedOut) {
          connectionStatus.state = "disconnected";
          connectionStatus.user = null;
          connectionStatus.lastMessage = "Desconectado (Sessão encerrada). Clique em Reconectar para gerar novo QR Code.";
          try {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
          } catch (e) {}
        } else if (shouldReconnect) {
          connectionStatus.state = "connecting";
          connectionStatus.lastMessage = "Reconectando ao WhatsApp...";
          setTimeout(() => {
            isStarting = false;
            startWhatsApp();
          }, 2500);
          return;
        }
      }
    });

    // Listen for full history sync (from first conversation to last, including INITIAL_BOOTSTRAP, RECENT, and FULL)
    sock.ev.on("messaging-history.set", ({ chats, contacts, messages, lidPnMappings, progress, syncType }) => {
      store.syncHistoryBatches += 1;
      store.lastSyncAt = new Date().toISOString();
      if (typeof progress === "number") {
        connectionStatus.syncProgress = progress;
      }

      if (Array.isArray(lidPnMappings)) {
        for (const m of lidPnMappings) {
          if (m && m.lid && m.pn) registerLidPnMapping(m.lid, m.pn);
        }
      }

      if (Array.isArray(chats)) {
        for (const ch of chats) upsertChat(ch);
      }
      if (Array.isArray(contacts)) {
        for (const c of contacts) upsertHistoryContact(c);
      }
      if (Array.isArray(messages)) {
        for (const m of messages) processMessage(m);
      }

      console.log(
        `[HistorySync batch #${store.syncHistoryBatches}] syncType=${syncType} progress=${progress}% -> chats=${Object.keys(store.chats).length}, contacts=${Object.keys(store.contacts).length}`
      );
      connectionStatus.lastMessage = `Sincronizado: ${Object.keys(store.chats).length} conversas individuais e ${Object.keys(store.contacts).length} contatos mapeados`;
      scheduleSave();
    });

    sock.ev.on("contacts.upsert", (contacts) => {
      for (const c of contacts) upsertAddressBookContact(c);
      store.lastSyncAt = new Date().toISOString();
      scheduleSave();
    });

    sock.ev.on("contacts.update", (updates) => {
      for (const u of updates) upsertHistoryContact(u);
      store.lastSyncAt = new Date().toISOString();
      scheduleSave();
    });

    sock.ev.on("chats.upsert", (chats) => {
      for (const ch of chats) upsertChat(ch);
      store.lastSyncAt = new Date().toISOString();
      scheduleSave();
    });

    sock.ev.on("chats.update", (updates) => {
      for (const ch of updates) upsertChat(ch);
      store.lastSyncAt = new Date().toISOString();
      scheduleSave();
    });

    sock.ev.on("labels.association", ({ association }) => {
      if (association && association.chatId) {
        upsertChat({ id: association.chatId });
        store.lastSyncAt = new Date().toISOString();
        scheduleSave();
      }
    });

    sock.ev.on("messages.upsert", ({ messages }) => {
      if (Array.isArray(messages)) {
        for (const m of messages) processMessage(m);
        store.lastSyncAt = new Date().toISOString();
        scheduleSave();
      }
    });
  } catch (err) {
    console.error("Failed to start WhatsApp socket:", err);
    connectionStatus.state = "disconnected";
    connectionStatus.lastMessage = "Erro ao iniciar conexão: " + err.message;
  } finally {
    isStarting = false;
  }
}

// Local HTTP Control Server on 127.0.0.1:3847
const server = http.createServer(async (req, res) => {
  res.setHeader("Content-Type", "application/json");
  const url = new URL(req.url, "http://127.0.0.1:3847");

  if (req.method === "GET" && url.pathname === "/status") {
    res.end(
      JSON.stringify({
        ...connectionStatus,
        stats: {
          totalIndividualContacts: Object.keys(store.contacts).length,
          totalIndividualChats: Object.keys(store.chats).length,
          totalLidMappings: Object.keys(store.lidToPn).length,
          syncHistoryBatches: store.syncHistoryBatches,
          totalMessagesScanned: store.totalMessagesScanned,
          lastSyncAt: store.lastSyncAt,
        },
      })
    );
    return;
  }

  if (req.method === "GET" && url.pathname === "/data") {
    res.end(
      JSON.stringify({
        user: connectionStatus.user,
        contacts: store.contacts,
        chats: store.chats,
        lidMeta: store.lidMeta,
        pnToLid: store.pnToLid,
        syncHistoryBatches: store.syncHistoryBatches,
        totalMessagesScanned: store.totalMessagesScanned,
        lastSyncAt: store.lastSyncAt,
      })
    );
    return;
  }

  if (req.method === "POST" && url.pathname === "/logout") {
    try {
      if (sock) {
        await sock.logout().catch(() => {});
        sock = null;
      }
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
      hasScannedAuthDirOnce = false;
      store = {
        contacts: {},
        chats: {},
        lidToPn: {},
        pnToLid: {},
        lidMeta: {},
        syncHistoryBatches: 0,
        totalMessagesScanned: 0,
        lastSyncAt: null,
      };
      scheduleSave();
      connectionStatus = {
        state: "disconnected",
        qrDataUrl: null,
        user: null,
        syncProgress: 0,
        lastMessage: "Sessão desconectada. Iniciando novo QR Code...",
      };
      setTimeout(() => startWhatsApp(), 1000);
      res.end(JSON.stringify({ ok: true }));
    } catch (e) {
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

  if (req.method === "POST" && url.pathname === "/reconnect") {
    try {
      if (sock) {
        sock.end(undefined);
        sock = null;
      }
    } catch (e) {}
    isStarting = false;
    startWhatsApp();
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  res.statusCode = 404;
  res.end(JSON.stringify({ error: "Not found" }));
});

const PORT = 3847;
server.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    console.log(`Port ${PORT} is already in use by an active bridge instance. Exiting gracefully.`);
    process.exit(0);
  } else {
    console.error("Bridge server error:", err);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`WhatsApp Bridge listening on http://127.0.0.1:${PORT}`);
  startWhatsApp();
});
