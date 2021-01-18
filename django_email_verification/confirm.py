from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as b64Error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from smtplib import SMTP
from threading import Thread

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import get_resolver
from django.utils import timezone

from .errors import InvalidUserModel, EmailTemplateNotFound, NotAllFieldCompiled
from .token import default_token_generator


def send_email(user, **kwargs):
    active_field = _get_validated_field('EMAIL_ACTIVE_FIELD')
    try:
        setattr(user, active_field, False)
        user.save()

        if kwargs.get('custom_salt'):
            default_token_generator.key_salt = kwargs['custom_salt']

        try:
            token = kwargs['token']
        except KeyError:
            token, expiry = default_token_generator.make_token(user)

        email = urlsafe_b64encode(str(user.email).encode('utf-8'))
        t = Thread(target=send_email_thread, args=(user.email, f'{email.decode("utf-8")}/{token}', expiry))
        t.start()
    except AttributeError:
        raise InvalidUserModel('The user model you provided is invalid')


def send_email_thread(email, token, expiry):
    email_server = _get_validated_field('EMAIL_SERVER')
    sender = _get_validated_field('EMAIL_FROM_ADDRESS')
    domain = _get_validated_field('EMAIL_PAGE_DOMAIN')
    subject = _get_validated_field('EMAIL_MAIL_SUBJECT')
    address = _get_validated_field('EMAIL_ADDRESS')
    port = _get_validated_field('EMAIL_PORT', default_type=int)
    password = _get_validated_field('EMAIL_PASSWORD')
    mail_plain = _get_validated_field('EMAIL_MAIL_PLAIN', raise_error=False)
    mail_html = _get_validated_field('EMAIL_MAIL_HTML', raise_error=False)

    if not (mail_plain or mail_html):  # Validation for mail_plain and mail_html as both of them have raise_error=False
        raise NotAllFieldCompiled(
            f"Both EMAIL_MAIL_PLAIN and EMAIL_MAIL_HTML missing from settings.py, at least one of them is required.")

    domain += '/' if not domain.endswith('/') else ''

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = email

    from .views import verify
    link = ''
    for k, v in get_resolver(None).reverse_dict.items():
        if k is verify and v[0][0][1][0]:
            addr = str(v[0][0][0])
            link = domain + addr[0: addr.index('%')] + token

    context = {'link': link, 'expiry': expiry}

    if mail_plain:
        try:
            text = render_to_string(mail_plain, context)
            part1 = MIMEText(text, 'plain')
            msg.attach(part1)
        except AttributeError:
            pass

    if mail_html:
        try:
            html = render_to_string(mail_html, context)
            part2 = MIMEText(html, 'html')
            msg.attach(part2)
        except AttributeError:
            pass

    if not msg.get_payload():
        raise EmailTemplateNotFound('No email template found')

    email_backend = _get_validated_field('EMAIL_BACKEND')

    if email_backend and email_backend == 'django.core.mail.backends.console.EmailBackend':
        msg = EmailMessage(subject, msg.as_string(), sender, [email])
        if mail_html:
            msg.content_subtype = "html"
        msg.send()
    else:
        server = SMTP(email_server, port)
        server.starttls()
        server.login(address, password)
        server.sendmail(sender, email, msg.as_string())
        server.quit()


def _get_validated_field(field, raise_error=True, default_type=None):
    if default_type is None:
        default_type = str
    try:
        d = getattr(settings, field)
        if d == "" or d is None or not isinstance(d, default_type):
            raise AttributeError
        return d
    except AttributeError:
        if raise_error:
            raise NotAllFieldCompiled(f"Field {field} missing or invalid")
        return None


def verify_token(email, email_token):
    try:
        users = get_user_model().objects.filter(email=urlsafe_b64decode(email).decode("utf-8"))
        for user in users:
            valid = default_token_generator.check_token(user, email_token)
            if valid:
                active_field = _get_validated_field('EMAIL_ACTIVE_FIELD')
                setattr(user, active_field, True)
                user.last_login = timezone.now()
                user.save()
                return valid
    except b64Error:
        pass
    return False