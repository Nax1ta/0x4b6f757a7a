"""SMTP submission with per-recipient receipts and stable message ids."""
import hashlib
import html
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate


def recipients(rid):
    text = os.getenv("BILIBILI_ROOM_EMAIL_TO_" + rid) or os.getenv("EMAIL_TO", "")
    result = list(dict.fromkeys(x.strip() for x in text.split(",") if x.strip()))
    if not result or any("@" not in x or "\n" in x or "\r" in x for x in result):
        raise ValueError("未配置有效收件人")
    return result


def ports():
    host = os.getenv("EMAIL_HOST", "smtp.163.com")
    candidates = [int(os.getenv("EMAIL_PORT", "465"))]
    candidates += [465, 994] if host in ("smtp.163.com", "smtp.126.com") else [465, 587]
    return list(dict.fromkeys(candidates))


def connect(port):
    host = os.getenv("EMAIL_HOST", "smtp.163.com")
    user, password = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASSWORD")
    if not user or not password:
        raise ValueError("缺少发件账号或授权码")
    context = ssl.create_default_context()
    session = None
    try:
        if port in (465, 994):
            session = smtplib.SMTP_SSL(host, port, timeout=8, context=context)
        else:
            session = smtplib.SMTP(host, port, timeout=8)
            session.ehlo()
            session.starttls(context=context)
        session.ehlo()
        session.login(user, password)
        return session
    except Exception:
        if session:
            session.close()
        raise


def probe():
    errors = []
    for port in ports():
        try:
            session = connect(port)
            session.close()
            return port
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError("SMTP 认证失败，请更新授权码") from None
        except Exception as exc:
            errors.append(str(port) + ":" + type(exc).__name__)
    raise RuntimeError("SMTP 不可用 " + ", ".join(errors))


def cover_image(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".hdslb.com"):
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=5) as r:
            data = r.read(2_000_001)
            content_type = r.headers.get_content_type()
        if len(data) > 2_000_000:
            return None
        if content_type == "image/webp":
            from io import BytesIO
            from PIL import Image
            buffer = BytesIO()
            Image.open(BytesIO(data)).convert("RGB").save(buffer, "JPEG", quality=85)
            return buffer.getvalue(), "jpeg"
        if content_type in ("image/jpeg", "image/png", "image/gif"):
            return data, content_type.split("/")[1]
    except Exception:
        pass
    return None


def build_message(room, recipient):
    msg = EmailMessage()
    sender = os.getenv("EMAIL_FROM") or os.getenv("EMAIL_USER", "")
    msg["From"] = formataddr(("开播提醒", sender))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=False)
    msg["Subject"] = "[开播] %s · %s" % (room["name"], room.get("title") or "直播中")
    digest = hashlib.sha256((room["id"] + room.get('message_session', room["session"]) + recipient.casefold()).encode()).hexdigest()
    msg["Message-ID"] = "<%s@%s>" % (digest, sender.split("@")[-1] or "localhost")
    msg.set_content("%s 开播了\n%s\n%s\n开播时间：%s" % (room["name"], room.get("title", ""), room["url"], room.get("last_live_at") or "未知"))
    image = cover_image(room.get("cover", ""))
    body = '<div style="font:15px/1.8 sans-serif;color:#252525;max-width:600px"><p><b>%s</b> 开播了</p><h2>%s</h2><p><a href="%s">进入直播间</a></p>%s<p style="color:#777">开播时间：%s</p></div>' % (
        html.escape(room["name"]), html.escape(room.get("title") or "直播中"), html.escape(room["url"], quote=True),
        '<img src="cid:cover" style="width:100%;max-width:600px" alt="直播封面">' if image else "",
        html.escape(room.get("last_live_at") or "未知"))
    msg.add_alternative(body, subtype="html")
    if image:
        msg.get_payload()[-1].add_related(image[0], maintype="image", subtype=image[1], cid="<cover>")
    return msg


def send(room, recipient):
    msg = build_message(room, recipient)
    errors = []
    for port in ports():
        session = None
        try:
            session = connect(port)
            refused = session.send_message(msg, from_addr=os.environ["EMAIL_USER"], to_addrs=[recipient])
            if refused:
                raise RuntimeError("收件人被 SMTP 拒绝")
            return {"port": port, "message_id": msg["Message-ID"], "result": "accepted"}
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError("SMTP 认证失败，请更新授权码") from None
        except Exception as exc:
            code = getattr(exc, 'smtp_code', None)
            reason = getattr(exc, 'smtp_error', b'')
            if isinstance(reason, bytes):
                reason = reason.decode('utf-8', 'replace')
            # Details are retained only in encrypted state, never public step logs.
            errors.append(str(port) + ":" + type(exc).__name__ + (" [%s] %s" % (code, str(reason)[:300]) if code else ""))
        finally:
            if session:
                session.close()
    raise RuntimeError("SMTP 提交失败 " + ", ".join(errors))
