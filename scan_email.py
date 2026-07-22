#!/usr/bin/env python3
"""
scan_email.py
-------------
Lee una casilla IMAP donde llegan las alertas de LinkedIn / Computrabajo /
Bumeran / ZonaJobs / Indeed (configuradas por vos en cada sitio), extrae las
ofertas de esos mails, las filtra por palabras clave y actualiza jobs.json
para que el dashboard (index.html) las muestre.

IMPORTANTE:
- Esto NO scrapea las páginas de empleo. Lee correos que los propios portales
  te mandan porque vos configuraste la alerta ahí. Es la forma legal y estable
  de hacer esto: si scrapeás LinkedIn directo te banean la cuenta o la IP.
- El formato de esos mails cambia con el tiempo y varía por portal. Los
  extractores de abajo son heurísticos (best effort). Si notás que a un
  portal no le está sacando bien el título/empresa, revisá un mail real de
  ese portal y ajustá la función extract_<portal> correspondiente.
"""

import imaplib
import email
from email.header import decode_header
import json
import re
import os
import sys
import hashlib
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


def decode_mime(s):
    if s is None:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
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
                decoded = payload.decode(charset, errors="ignore")
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
            return payload.decode(charset, errors="ignore") if payload else ""
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


# ---------------------------------------------------------------------------
# Extractores por portal (heurísticos: ajustar según los mails reales)
# ---------------------------------------------------------------------------

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
    # Todos usan el mismo fallback genérico por ahora. Si querés precisión
    # por portal, agregá acá un regex específico basado en un mail real.
    "linkedin": extract_generic,
    "computrabajo": extract_generic,
    "bumeran": extract_generic,
    "zonajobs": extract_generic,
    "indeed": extract_generic,
    "otro": extract_generic,
}


def keyword_match(title, snippet, config):
    text = (title + " " + snippet).lower()
    include = [k.lower() for k in config.get("keywords_include", [])]
    exclude = [k.lower() for k in config.get("keywords_exclude", [])]

    if exclude and any(k in text for k in exclude):
        return None
    if not include:
        return []  # sin filtro de inclusión -> todo pasa, sin keywords marcadas
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
                continue  # no matchea keywords, o cayó en exclude

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
