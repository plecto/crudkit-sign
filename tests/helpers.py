import io
import re

from django.contrib.auth import get_user_model
from django.core import mail
from PIL import Image

from tests.testapp.models import Memo

SIGN_URL = re.compile(r"https://crm\.example\.com/sign/([A-Za-z0-9_-]+)/")


def make_user(username="staff"):
    return get_user_model().objects.create_user(username, f"{username}@example.com", "password")


def make_memo(user, anchors=None, signers=None, pages=1, title="Order form"):
    anchors = anchors if anchors is not None else ["{{sig:customer}}", "{{date:customer}}", "{{name:customer}}"]
    signers = signers if signers is not None else [{"role": "customer", "name": "Ada", "email": "ada@example.com"}]
    return Memo.objects.create(
        title=title, anchors=anchors, signer_specs=signers, pages=pages, created_by=user, updated_by=user
    )


def png_bytes(width=40, height=20):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(width):
        image.putpixel((x, height // 2), (17, 24, 39, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tokens_from_outbox():
    """Signer email -> token, from every invite in mail.outbox (later mails win)."""
    tokens = {}
    for message in mail.outbox:
        match = SIGN_URL.search(message.body)
        if match:
            for recipient in message.to:
                tokens[recipient] = match.group(1)
    return tokens


def last_token(email):
    return tokens_from_outbox()[email]


def hooks(memo):
    return Memo.objects.get(pk=memo.pk).hook_calls
