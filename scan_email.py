#!/usr/bin/env python3
"""
scan_email.py
-------------
Lee una casilla IMAP donde llegan las alertas de LinkedIn / Computrabajo /
Bumeran / ZonaJobs / Indeed (configuradas por vos en cada sitio), extrae las
ofertas de esos mails, las filtra por palabras clave y actualiza jobs.json
para que el dashboard (index.html) las muestre.
"""

import imaplib
import email
from email.header import decode_header
import json
import re
import os
import sys
import hashlib
import unicodedata
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("JOB_ALERT_CONFIG", "config.json")
JOBS_PATH = os.environ.get("JOB_ALERT_JOBS", "jobs.json")
SEEN_PATH = os.environ.get("JOB_ALERT_SEEN", "seen.json")
MAX_JOBS_STORED = 300


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"No encontré {CONFIG_PATH}. Copiá config.example.json a config.json y completalo.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def safe_decode(raw_bytes, enc):
    """Decodifica bytes probando el encoding indicado, con varios fallbacks
    para encodings raras/legacy que a veces mandan estos portales
    (ej. 'unknown-8bit', 'x-user-defined', etc.)."""
    candidates = [enc, "utf-8", "latin-1"]
    for c in candidates:
        if not c:
            continue
        try:
            return raw_bytes.decode(c, errors="ignore")
        except (LookupError, TypeError):
            continue
    # último recurso: nunca debería fallar esto
    return raw_bytes.decode("utf-8", errors="replace")


def decode_mime(s):
    if s is None:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += safe_decode(text, enc)
        else:
            out += text
    return out


def get_body(msg):
    """Devuelve el cuerpo en texto plano (o HTML como fallback) del mail."""
    if msg.is_multipart():
        text_plain, text_html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = safe_decode(payload, charset)
            except Exception:
                continue
            if ctype == "text/plain":
                text_plain += decoded
            elif ctype == "text/html":
                text_html += decoded
        return text_plain or text_html
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            return safe_decode(payload, charset) if payload else ""
        except Exception:
            return ""


def strip_html(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def find_urls(text):
    return re.findall(r"https?://[^\s\"'<>]+", text)


def detect_source(from_addr):
    from_addr = from_addr.lower()
    if "linkedin" in from_addr:
        return "linkedin"
    if "computrabajo" in from_addr:
        return "computrabajo"
    if "bumeran" in from_addr:
        return "bumeran"
    if "zonajobs" in from_addr:
        return "zonajobs"
    if "indeed" in from_addr:
        return "indeed"
    return "otro"


def extract_generic(subject, text, urls):
    """Fallback: usa el asunto como título y la primera URL como link."""
    title = subject
    company = ""
    m = re.search(r"(.+?)\s+(?:en|at|-)\s+(.+)", subject)
    if m:
        title, company = m.group(1).strip(), m.group(2).strip()
    return {
        "title": title,
        "company": company or "No especificada",
        "url": urls[0] if urls else "",
        "snippet": text[:300].replace("\n", " ").strip(),
    }


EXTRACTORS = {
    "linkedin": extract_generic,
    "computrabajo": extract_generic,
    "bumeran": extract_generic,
    "zonajobs": extract_generic,
    "indeed": extract_generic,
    "otro": extract_generic,
}


def normalize(s):
    """Saca tildes/acentos para que 'administracion' matchee 'Administración'."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def keyword_match(title, snippet, config):
    text = normalize(title + " " + snippet)
    include = [normalize(k) for k in config.get("keywords_include", [])]
    exclude = [normalize(k) for k in config.get("keywords_exclude", [])]

    if exclude and any(k in text for k in exclude):
        return None
    if not include:
        return []
    matched = [k for k in include if k in text]
    return matched if matched else None


def make_id(source, title, company, url):
    raw = f"{source}|{title}|{company}|{url}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def main():
    config = load_config()
    seen = load_json(SEEN_PATH, [])
    seen_set = set(seen)
    jobs = load_json(JOBS_PATH, [])

    imap_host = config["imap_host"]
    imap_user = os.environ.get("IMAP_USER", config.get("imap_user", ""))
    imap_pass = os.environ.get("IMAP_PASS", config.get("imap_pass", ""))
    senders = config.get("senders", [])

    if not imap_user or not imap_pass:
        sys.exit("Falta IMAP_USER / IMAP_PASS (como variable de entorno o en config.json).")

    print(f"Conectando a {imap_host} como {imap_user}...")
    M = imaplib.IMAP4_SSL(imap_host)
    M.login(imap_user, imap_pass)
    M.select("INBOX")

    new_jobs = []

    for sender in senders:
        typ, data = M.search(None, f'(FROM "{sender}" UNSEEN)')
        if typ != "OK":
            continue
        ids = data[0].split()
        print(f"  {sender}: {len(ids)} mail(es) nuevo(s)")
        for mail_id in ids:
            try:
                typ, msg_data = M.fetch(mail_id, "(RFC822)")
                if typ != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = decode_mime(msg.get("Subject"))
                from_addr = decode_mime(msg.get("From"))
                raw_body = get_body(msg)
                is_html = "<html" in raw_body.lower() or "<body" in raw_body.lower()
                text = strip_html(raw_body) if is_html else raw_body
                urls = find_urls(raw_body)

                source = detect_source(from_addr)
                extractor = EXTRACTORS.get(source, extract_generic)
                extracted = extractor(subject, text, urls)

                matched = keyword_match(extracted["title"], extracted["snippet"], config)
                if matched is None:
                    print(f"    (descartado por keywords) {extracted['title'][:80]}")
                    continue

                job_id = make_id(source, extracted["title"], extracted["company"], extracted["url"])
                if job_id in seen_set:
                    continue

                job = {
                    "id": job_id,
                    "title": extracted["title"],
                    "company": extracted["company"],
                    "source": source,
                    "url": extracted["url"],
                    "matched_keywords": matched,
                    "found_at": datetime.now(timezone.utc).isoformat(),
                    "snippet": extracted["snippet"],
                    "status": "new",
                }
                new_jobs.append(job)
                seen_set.add(job_id)
            except Exception as e:
                # Un mail individual con formato raro no debe tirar abajo todo el escaneo
                print(f"    (error procesando un mail, lo salteo: {e})")
                continue

    M.logout()

    if new_jobs:
        jobs = new_jobs + jobs
        jobs = jobs[:MAX_JOBS_STORED]
        save_json(JOBS_PATH, jobs)
        save_json(SEEN_PATH, list(seen_set))
        print(f"✅ {len(new_jobs)} oferta(s) nueva(s) agregada(s) a {JOBS_PATH}")
    else:
        print("Sin ofertas nuevas que matcheen tus keywords.")


if __name__ == "__main__":
    main()
