import requests 
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from allauth.socialaccount.providers.orcid.provider import OrcidProvider
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from utils.signer import decode_signed_value, encode_signed_value
from paper.models import Paper
from urllib.parse import urlencode 
from datetime import timedelta
from urllib.parse import urlencode, urlparse 
from paper.openalex_util import process_openalex_works
from paper.related_models.authorship_model import Authorship
from user.related_models.author_model import Author
from utils.openalex import OpenAlex
 

ORCID_BASE_URL = "https://orcid.org"
ORCID_API_URL = "https://pub.orcid.org/v3.0"
STATE_MAX_AGE = 600


def is_valid_redirect_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in settings.CORS_ORIGIN_WHITELIST

def get_orcid_app():
    return SocialApp.objects.get(provider=OrcidProvider.id)


def is_orcid_connected(user):
    if not user:
        return False
    return SocialAccount.objects.filter(user=user, provider=OrcidProvider.id).exists()


def decode_state(state):
    return decode_signed_value(state, max_age=STATE_MAX_AGE)


def exchange_code_for_token(app, code):
    response = requests.post(
        f"{ORCID_BASE_URL}/oauth/token",
        headers={"Accept": "application/json"},
        data={
            "client_id": app.client_id,
            "client_secret": app.secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.ORCID_REDIRECT_URL,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def connect_orcid_account(user, token_data, app):
    if "orcid" not in token_data:
        raise ValueError("Invalid ORCID response")

    orcid_id = token_data["orcid"]
    already_linked = (
        SocialAccount.objects
        .filter(provider=OrcidProvider.id, uid=orcid_id)
        .exclude(user=user)
        .exists()
    )
    if already_linked:
        raise ValueError("ORCID already linked to another account")

    extra_data = {
        "name": token_data.get("name", ""),
        "scope": token_data.get("scope", ""),
    }

    account, _ = SocialAccount.objects.update_or_create(
        user=user,
        provider=OrcidProvider.id,
        defaults={"uid": orcid_id, "extra_data": extra_data},
    )

    expires_at = None
    if expires_in := token_data.get("expires_in"):
        expires_at = timezone.now() + timedelta(seconds=expires_in)

    SocialToken.objects.update_or_create(
        account=account,
        app=app,
        defaults={
            "token": token_data.get("access_token", ""),
            "token_secret": token_data.get("refresh_token", ""),
            "expires_at": expires_at,
        },
    )

    if author := getattr(user, "author_profile", None):
        author.orcid_id = f"{ORCID_BASE_URL}/{orcid_id}"
        author.save(update_fields=["orcid_id"])


def build_auth_url(app, user_id, return_url=None):
    state_data = {"user_id": user_id}
    if is_valid_redirect_url(return_url):
        state_data["return_url"] = return_url
    params = {
        "client_id": app.client_id,
        "response_type": "code",
        "scope": "/authenticate",
        "redirect_uri": settings.ORCID_REDIRECT_URL,
        "state": encode_signed_value(state_data),
    }
    return f"{ORCID_BASE_URL}/oauth/authorize?{urlencode(params)}"

def get_redirect_url(error=None, return_url=None):
    base = return_url if is_valid_redirect_url(return_url) else settings.BASE_FRONTEND_URL
    separator = "&" if "?" in base else "?"
    if error:
        return f"{base}{separator}orcid_error={error}"
    return f"{base}{separator}orcid_connected=true"

def extract_orcid_id(orcid_url):
    if not orcid_url:
        return None
    return orcid_url.replace(f"{ORCID_BASE_URL}/", "").strip("/")


def fetch_orcid_works(orcid_id):
    response = requests.get(
        f"{ORCID_API_URL}/{orcid_id}/works",
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def extract_dois_from_orcid_works(orcid_data):
    dois = []
    for group in orcid_data.get("group", []):
        for summary in group.get("work-summary", []):
            for ext_id in summary.get("external-ids", {}).get("external-id", []):
                if ext_id.get("external-id-type") == "doi":
                    doi = ext_id.get("external-id-value")
                    if doi:
                        dois.append(doi)
                    break
    return dois


def sync_orcid_papers(author_id):
    author = Author.objects.get(id=author_id)
    orcid_id = extract_orcid_id(author.orcid_id)

    if not orcid_id:
        raise ValueError("Author has no ORCID connected")

    orcid_data = fetch_orcid_works(orcid_id)
    dois = extract_dois_from_orcid_works(orcid_data)

    if not dois:
        return {"papers_processed": 0, "author_id": author_id}

    openalex = OpenAlex()
    works = []
    for doi in dois:
        work = openalex.get_work_by_doi(doi)
        if work:
            works.append(work)

    if works:
        process_openalex_works(works)

    linked_count = link_papers_to_author(author, works)
    return {"papers_processed": linked_count, "author_id": author_id}


def get_author_position_from_work(work, orcid_id):
    for authorship in work.get("authorships", []):
        author_data = authorship.get("author", {})
        if author_data.get("orcid") == f"{ORCID_BASE_URL}/{orcid_id}":
            return authorship.get("author_position", Authorship.MIDDLE_AUTHOR_POSITION)
    return Authorship.MIDDLE_AUTHOR_POSITION


def link_papers_to_author(author, works):
    orcid_id = extract_orcid_id(author.orcid_id)
    linked = 0

    for work in works:
        raw_doi = work.get("doi", "")
        if not raw_doi:
            continue

        clean_doi = raw_doi.replace("https://doi.org/", "")
        paper = Paper.objects.filter(
            Q(doi__iexact=raw_doi) | Q(doi__iexact=clean_doi)
        ).first()
        if not paper:
            continue

        position = get_author_position_from_work(work, orcid_id)

        _, created = Authorship.objects.get_or_create(
            paper=paper,
            author=author,
            defaults={"author_position": position},
        )
        if created:
            linked += 1

    if linked > 0:
        cache.delete(f"author-{author.id}-publications")

    return linked

