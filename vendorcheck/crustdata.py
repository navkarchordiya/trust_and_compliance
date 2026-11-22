"""Thin Crustdata client. Endpoints and fields follow docs.crustdata.com (API version 2025-11-01)."""
import os

from .sources import Result, SourceClient


def _no_matches(data) -> bool:
    return isinstance(data, list) and all(not (item or {}).get("matches") for item in data)


class Crustdata:
    def __init__(self, client: SourceClient, cfg: dict):
        self.client = client
        self.base = cfg["crustdata"]["base_url"].rstrip("/")
        self.cfg = cfg
        self.headers = {
            "authorization": f"Bearer {os.environ.get('CRUSTDATA_API_KEY', '')}",
            "content-type": "application/json",
            "x-api-version": cfg["crustdata"]["api_version"],
        }

    def _post(self, endpoint: str, payload: dict, not_found=None) -> Result:
        return self.client.call("crustdata", endpoint, "POST", self.base + endpoint, payload,
                                self.headers, not_found=not_found)

    # 0.2 credits per matched identifier
    def identify(self, domain: str | None = None, name: str | None = None) -> Result:
        payload = {"domains": [domain]} if domain else {"names": [name]}
        return self._post("/company/identify", payload, not_found=_no_matches)

    # 2 credits per record
    def enrich_company(self, company_id: int) -> Result:
        return self._post("/company/enrich", {
            "crustdata_company_ids": [company_id],
            "fields": self.cfg["crustdata"]["enrich_fields"],
        }, not_found=_no_matches)

    # 1+ credits per record
    def enrich_person(self, profile_url: str) -> Result:
        return self._post("/person/enrich", {
            "professional_network_profile_urls": [profile_url],
            "fields": ["basic_profile", "experience"],
        }, not_found=_no_matches)

    # 1 credit per page
    def web_search(self, query: str, sources=("news",), location: str | None = None) -> Result:
        payload = {"query": query, "sources": list(sources)}
        if location:
            payload["location"] = location
        return self._post("/web/search/live", payload,
                          not_found=lambda d: isinstance(d, dict) and not d.get("results"))

    # 1 credit per page, up to 10 urls per request
    def web_fetch(self, url: str) -> Result:
        return self._post("/web/enrich/live", {"urls": [url]},
                          not_found=lambda d: isinstance(d, list) and not any(x.get("success") for x in d))


def rdap_domain(client: SourceClient, domain: str) -> Result:
    """Domain registration record from the public RDAP bootstrap (free, no key)."""
    return client.call("rdap", "rdap", "GET", f"https://rdap.org/domain/{domain}",
                       headers={"accept": "application/rdap+json"})


def gst_verify(client: SourceClient, cfg: dict, gstin: str) -> Result | None:
    """GSTIN lookup in the GST registry through a KYC provider. Returns None when no provider key
    is configured (the check is then reported as 'cannot verify', never as a pass).

    Cashfree Secure ID: POST {base}/gstin  {"GSTIN": ...}  with x-client-id / x-client-secret.
    Fields used: valid, gst_in_status, legal_name_of_business, trade_name_of_business,
    date_of_registration, constitution_of_business. Run one real call and adjust if they differ.
    """
    rcfg = cfg.get("registry", {})
    if rcfg.get("provider") != "cashfree":
        return None
    client_id, secret = os.environ.get("CASHFREE_CLIENT_ID"), os.environ.get("CASHFREE_CLIENT_SECRET")
    if not (client_id and secret):
        return None
    url = rcfg["cashfree_base_url"].rstrip("/") + "/gstin"
    headers = {"x-client-id": client_id, "x-client-secret": secret, "content-type": "application/json"}
    return client.call("gst", "/gstin", "POST", url, {"GSTIN": gstin}, headers)
