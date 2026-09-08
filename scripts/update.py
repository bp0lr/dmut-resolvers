"""Prepare a resolver update in a new directory; never modify published files."""

import argparse
import hashlib
from http.client import HTTPException, IncompleteRead
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib import error, parse, request


LISTS = ("resolvers.txt", "top20.txt")
PUBLISHED = (*LISTS, "metadata.json")


class UpdateError(Exception):
    """An actionable failure that must prevent publication."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise UpdateError("Configuration must be a JSON object.")
    for key, low, high in (
        ("max_source_bytes", 1, 32 * 1024 * 1024),
        ("max_candidates", 1, 20000), ("minimum_resolvers", 1, 20000),
        ("tests", 1, 5000), ("workers", 1, 251), ("qps", 1, 50),
        ("timeout_seconds", 1, 10), ("max_duration_seconds", 1, 10800),
    ):
        value = config.get(key)
        if type(value) is not int or not low <= value <= high:
            raise UpdateError(f"Configuration: {key} must be an integer between {low} and {high}.")
    for key, low, high in (
        ("maximum_drop_fraction", 0, 0.99),
        ("minimum_success_percent", 1, 100), ("maximum_p95_ms", 1, 10000),
    ):
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise UpdateError(f"Configuration: invalid {key}.")
    if config["minimum_resolvers"] > config["max_candidates"]:
        raise UpdateError("Configuration: minimum_resolvers exceeds max_candidates.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", config.get("go_version", "")):
        raise UpdateError("Configuration: pin a complete Go version.")
    if not re.fullmatch(r"[a-f0-9]{40}", config.get("dnsfaster_ref", "")):
        raise UpdateError("Configuration: pin dnsfaster to a full commit SHA.")
    url = parse.urlsplit(config.get("source_url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise UpdateError("Configuration: source_url must be HTTPS without credentials.")
    domain = config.get("domain", "")
    if not domain or len(domain) > 253 or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        raise UpdateError("Configuration: invalid validation domain.")
    return config


class HTTPSRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = parse.urlsplit(newurl)
        if url.scheme != "https" or url.username or url.password:
            raise UpdateError("The source redirected to an unsafe URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit, log, *, opener=None, sleep=time.sleep):
    opener = opener or request.build_opener(HTTPSRedirect()).open
    for attempt in range(1, 4):
        try:
            req = request.Request(url, headers={"User-Agent": "dmut-resolvers-updater"})
            with opener(req, timeout=15) as response:
                if response.status != 200:
                    raise UpdateError(f"Source returned unexpected HTTP {response.status}.")
                expected_length = response.headers.get("Content-Length")
                if expected_length is not None:
                    try:
                        expected_length = int(expected_length)
                    except ValueError as exc:
                        raise UpdateError("Source returned an invalid Content-Length.") from exc
                    if expected_length < 0 or expected_length > limit:
                        raise UpdateError(f"Source exceeds the {limit}-byte limit or has an invalid length.")
                started = time.monotonic()
                data = bytearray()
                while True:
                    chunk = response.read1(min(65536, limit + 1 - len(data)))
                    data.extend(chunk)
                    if len(data) > limit:
                        raise UpdateError(f"Source exceeds the {limit}-byte limit.")
                    if time.monotonic() - started > 30:
                        raise TimeoutError("source body exceeded the download time budget")
                    if not chunk:
                        if expected_length is not None and len(data) != expected_length:
                            raise IncompleteRead(bytes(data), expected_length)
                        return bytes(data)
        except error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise UpdateError(f"Source returned HTTP {exc.code}; check source_url.") from exc
            reason = f"HTTP {exc.code}"
            exc.close()
        except (error.URLError, TimeoutError, ConnectionError, HTTPException) as exc:
            reason = str(exc)
        log.write(f"Download attempt {attempt}/3 failed: {reason}\n")
        log.flush()
        if attempt < 3:
            sleep(attempt * 2)
    raise UpdateError("Source download failed after 3 attempts; see download.log.")


def public_ipv4(value):
    # dnsfaster exports normalized endpoints with :53. dmut's legacy files use bare IPv4.
    if not isinstance(value, str):
        raise ValueError("Resolver must be a string.")
    if value.count(":") == 1 and value.endswith(":53"):
        value = value[:-3]
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_global or address.is_multicast:
        return None
    return str(address)


def parse_source(data, maximum):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise UpdateError("Source is not UTF-8 text.") from exc
    addresses, seen, skipped, duplicates = [], set(), 0, 0
    for number, line in enumerate(text.splitlines(), 1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            address = public_ipv4(value)
        except ValueError as exc:
            raise UpdateError(f"Invalid source address at line {number}; expected an IP address.") from exc
        if address is None:
            skipped += 1
        elif address in seen:
            duplicates += 1
        else:
            addresses.append(address)
            seen.add(address)
    if not addresses:
        raise UpdateError("Source contains no eligible public IPv4 resolvers.")
    if len(addresses) > maximum:
        raise UpdateError(f"Source has {len(addresses)} candidates, above the {maximum} limit; review the run budget.")
    return addresses, {"excluded_addresses": skipped, "duplicate_addresses": duplicates}


def list_bytes(addresses):
    return ("\n".join(addresses) + "\n").encode("ascii")


def strict_list(path):
    data = Path(path).read_bytes()
    addresses, counts = parse_source(data, 20000)
    if counts["excluded_addresses"] or counts["duplicate_addresses"] or list_bytes(addresses) != data:
        raise UpdateError(f"{Path(path).name} must contain unique bare public IPv4 addresses with LF endings.")
    return addresses


def select_results(records, candidates, config):
    if not isinstance(records, list) or len(records) != len(candidates):
        raise UpdateError("Incomplete dnsfaster report: expected one result per input resolver.")
    seen, passing = set(), []
    for record in records:
        if not isinstance(record, dict) or type(record.get("filtered")) is not bool:
            raise UpdateError("Invalid dnsfaster result schema.")
        try:
            address = public_ipv4(record["resolver"])
        except (ValueError, KeyError, TypeError) as exc:
            raise UpdateError("Invalid resolver in dnsfaster report.") from exc
        if address not in candidates or address in seen:
            raise UpdateError("Unexpected or duplicate resolver in dnsfaster report.")
        seen.add(address)
        if record["filtered"]:
            continue
        for field in ("successes", "failures", "validation_checks", "validation_failures"):
            if type(record.get(field)) is not int or record[field] < 0:
                raise UpdateError(f"Invalid {field} in dnsfaster report.")
        for field in ("p95_ms", "average_ms", "success_percent"):
            value = record.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise UpdateError(f"Invalid {field} in dnsfaster report.")
        expected_rate = 100 * record["successes"] / config["tests"]
        if (record["successes"] + record["failures"] != config["tests"]
                or record["validation_checks"] < 2 or record["validation_failures"] != 0
                or not math.isclose(record["success_percent"], expected_rate, abs_tol=0.001)
                or expected_rate < config["minimum_success_percent"]
                or record["p95_ms"] > config["maximum_p95_ms"]):
            raise UpdateError("A passing dnsfaster result does not satisfy the configured quality checks.")
        passing.append((record["p95_ms"], record["average_ms"], int(ipaddress.ip_address(address)), address))
    return [entry[3] for entry in sorted(passing)]


def check_count(addresses, previous, config):
    count = len(addresses)
    if count < config["minimum_resolvers"]:
        raise UpdateError(f"Only {count} resolvers passed; at least {config['minimum_resolvers']} are required.")
    # The pre-migration list has no measurement provenance. Do not treat it as a measured baseline.
    if (previous / "metadata.json").exists():
        old = strict_list(previous / "resolvers.txt")
        if count < len(old) * (1 - config["maximum_drop_fraction"]):
            raise UpdateError(f"Resolver count fell from {len(old)} to {count}, beyond the configured drop limit.")


def verify_bundle(candidate, previous, config):
    report = read_json(candidate / "report.json")
    if not isinstance(report, dict) or report.get("status") != "validated":
        raise UpdateError("Candidate report is not validated.")
    try:
        validated = datetime.fromisoformat(report["validated_at"])
        age = (datetime.now(timezone.utc) - validated).total_seconds()
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("Candidate has no valid UTC validation timestamp.") from exc
    if not -300 <= age <= 86400:
        raise UpdateError("Candidate is older than 24 hours or dated in the future; generate a fresh candidate.")
    if report.get("config_sha256") != digest(previous / "update-config.json"):
        raise UpdateError("Configuration changed since validation; generate a fresh candidate.")
    if not isinstance(report.get("sha256"), dict):
        raise UpdateError("Candidate checksums are missing.")
    for name in PUBLISHED:
        if digest(candidate / name) != report.get("sha256", {}).get(name) or not (candidate / name).is_file():
            raise UpdateError(f"Candidate checksum mismatch: {name}.")
    addresses = strict_list(candidate / "resolvers.txt")
    if strict_list(candidate / "top20.txt") != addresses[:20]:
        raise UpdateError("top20.txt must be the first up to 20 entries in resolvers.txt.")
    check_count(addresses, previous, config)
    return report


def summary(report):
    lines = ["## Resolver generation", "", f"Status: **{report['status']}**. Generation does not publish files.", ""]
    for key in ("validated_at", "duration_seconds", "source_url", "dnsfaster_version", "candidates", "passing", "added", "removed", "changed", "bootstrap", "error"):
        if key in report:
            value = str(report[key]).replace("\n", " ").replace("`", "'")
            lines.append(f"- {key.replace('_', ' ').capitalize()}: `{value}`")
    return "\n".join(lines) + "\n"


def prepare(repo, output, config, executable="dnsfaster"):
    # Reject reuse so a previous successful bundle can never survive a failed rerun.
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {"status": "failed", "source_url": config["source_url"], "config_sha256": digest(repo / "update-config.json"), "bootstrap": not (repo / "metadata.json").exists()}
    try:
        report["dnsfaster_version"] = subprocess.run(
            [executable, "--version"], check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        with (output / "download.log").open("w", encoding="utf-8") as log:
            source = download(config["source_url"], config["max_source_bytes"], log)
        (output / "source.txt").write_bytes(source)
        candidates, source_counts = parse_source(source, config["max_candidates"])
        report.update(source_counts, candidates=len(candidates))
        (output / "input.txt").write_bytes(list_bytes(candidates))
        # Override dnsfaster's unrelated default negative-check domains. Both
        # correctness checks and measurements use the configured validation domain.
        args = [executable, "--in", str(output / "input.txt"), "--out", str(output / "results.json"),
                "--format", "json", "--include-filtered", "--validation", "baseline", "--record-types", "A",
                "--domain", config["domain"], "--negative-domain", config["domain"],
                "--tests", str(config["tests"]), "--workers", str(config["workers"]),
                "--qps", str(config["qps"]), "--timeout", f"{config['timeout_seconds']}s",
                "--max-duration", f"{config['max_duration_seconds']}s", "--validation-retries", "1",
                "--filter-rate", str(config["minimum_success_percent"]), "--filter-p95", str(config["maximum_p95_ms"]), "--sort", "p95"]
        report["command"] = args
        with (output / "dnsfaster.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=config["max_duration_seconds"] + 30)
        if result.returncode != 0:
            raise UpdateError(f"dnsfaster exited with code {result.returncode}; see dnsfaster.log. No candidate is eligible for publication.")
        addresses = select_results(read_json(output / "results.json"), set(candidates), config)
        report["passing"] = len(addresses)
        check_count(addresses, repo, config)
        old = set((repo / "resolvers.txt").read_text(encoding="utf-8").splitlines())
        report.update(added=len(set(addresses) - old), removed=len(old - set(addresses)))
        (output / "resolvers.txt").write_bytes(list_bytes(addresses))
        (output / "top20.txt").write_bytes(list_bytes(addresses[:20]))
        report["changed"] = any(digest(output / name) != digest(repo / name) for name in LISTS)
        report["validated_at"] = datetime.now(timezone.utc).isoformat()
        metadata = {"schema_version": 1, "validated_at": report["validated_at"], "source_url": config["source_url"],
                    "source_sha256": hashlib.sha256(source).hexdigest(), "config": config,
                    "dnsfaster_version": report["dnsfaster_version"], "candidates": len(candidates), "passing": len(addresses),
                    "sha256": {name: digest(output / name) for name in LISTS}}
        write_json(output / "metadata.json", metadata)
        report["sha256"] = {name: digest(output / name) for name in PUBLISHED}
        report["status"] = "validated"
        return report
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        raise UpdateError(str(exc)) from exc
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        write_json(output / "report.json", report)
        text = summary(report)
        (output / "summary.md").write_text(text, encoding="utf-8")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/candidate"), help="New directory for the candidate and diagnostics")
    parser.add_argument("--dnsfaster", default="dnsfaster", help="Path to the installed dnsfaster binary")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        report = prepare(repo, args.output.resolve(), load_config(repo / "update-config.json"), args.dnsfaster)
    except (UpdateError, OSError, ValueError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    print(f"Validated {report['passing']} resolvers. Files changed: {report['changed']}. Preview: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
