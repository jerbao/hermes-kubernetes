"""Fixed upstream quota probes; OAuth tokens never leave CLIProxyAPI."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from hermes_kubernetes.quota.normalization import normalize

FAMILIES = {
    "codex": "codex",
    "claude": "claude",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "antigravity": "gemini",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, token=None, data=None, timeout=20):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        url, headers=headers, data=json.dumps(data).encode() if data is not None else None
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Quota response exceeds the size limit")
        return json.loads(raw)


class ProxyClient:
    def __init__(self, url, key, request=request_json):
        if url.rstrip("/") != "http://127.0.0.1:8317":
            raise ValueError("Management must use the local CLIProxyAPI sidecar")
        self.url, self.key, self.request = url.rstrip("/"), key, request

    def management(self, path, data=None):
        return self.request(self.url + "/v0/management/" + path, token=self.key, data=data)

    def probe(self, account, family):
        headers = {"Authorization": "Bearer $TOKEN$", "Accept": "application/json"}
        data = None
        if family == "codex":
            method, url = "GET", "https://chatgpt.com/backend-api/wham/usage"
            identity = account.get("id_token") or {}
            if isinstance(identity, dict) and identity.get("chatgpt_account_id"):
                headers["ChatGPT-Account-ID"] = str(identity["chatgpt_account_id"])
            headers["User-Agent"] = "codex-cli"
        elif family == "claude":
            method, url = "GET", "https://api.anthropic.com/api/oauth/usage"
            headers.update(
                {"anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"}
            )
        else:
            method, url = "POST", "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
            project = account.get("project_id")
            if not project:
                raise ValueError("Gemini project unavailable")
            data = json.dumps({"project": project})
            headers["Content-Type"] = "application/json"
            if (account.get("provider") or account.get("type")) == "antigravity":
                url = (
                    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
                )
                headers["User-Agent"] = (
                    "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=amd64)"
                )
                family = "antigravity"
        body = {
            "auth_index": account["auth_index"],
            "method": method,
            "url": url,
            "header": headers,
        }
        if data is not None:
            body["data"] = data
        response = self.management("api-call", body)
        status = response.get("status_code")
        if status != 200:
            raise ValueError(f"Provider quota HTTP {status}")
        return normalize(family, json.loads(response["body"]))

    def account(self, entry):
        family = FAMILIES.get(entry.get("provider") or entry.get("type"))
        if family is None or not entry.get("auth_index"):
            return None
        # Do not publish credential filenames, account emails, paths, raw responses or tokens.
        identifier = hashlib.sha256((family + ":" + str(entry["auth_index"])).encode()).hexdigest()[
            :20
        ]
        row = {
            "id": identifier,
            "label": family.title() + " " + identifier[:6],
            "windows": [],
            "state": "unknown",
            "resets_at": None,
        }
        if entry.get("disabled"):
            row.update(state="unavailable", reason="Credential disabled")
            return family, row
        try:
            row["windows"] = self.probe(entry, family)
            exhausted = [
                w for w in row["windows"] if w["scope"] == "account" and w["used_percent"] >= 100
            ]
            row["state"] = "limited" if exhausted else "ready" if row["windows"] else "unknown"
            if exhausted and all(w["resets_at"] for w in exhausted):
                row["resets_at"] = max(w["resets_at"] for w in exhausted)
            if not row["windows"]:
                row["reason"] = "Provider did not report quota windows"
        except (OSError, ValueError, KeyError, TypeError) as error:
            row.update(state="unavailable", reason="Quota query unavailable")
            if isinstance(error, ValueError) and str(error).startswith("Provider quota HTTP "):
                row["reason"] = str(error)
        return family, row

    def collect(self):
        entries = self.management("auth-files").get("files")
        if not isinstance(entries, list):
            raise ValueError("Invalid credential inventory")
        result = {family: {"accounts": []} for family in set(FAMILIES.values())}
        with ThreadPoolExecutor(max_workers=4) as executor:
            for item in executor.map(self.account, [e for e in entries if isinstance(e, dict)]):
                if item:
                    family, row = item
                    result[family]["accounts"].append(row)
        return {
            "schema": 1,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "providers": result,
        }
