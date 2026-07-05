#!/usr/bin/env python3
"""Fetch Jira issue data and linked issues. Outputs JSON to stdout."""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

def fetch_jira(issue_key: str, base_url: str = None, email: str = None, token: str = None) -> dict:
    base_url = base_url or os.environ.get("JIRA_BASE_URL", "")
    email = email or os.environ.get("JIRA_EMAIL", "")
    token = token or os.environ.get("JIRA_API_TOKEN", "")
    
    if not all([base_url, email, token]):
        print(json.dumps({"error": "Missing Jira credentials. Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN"}))
        sys.exit(1)
    
    issue_url = f"{base_url}/rest/api/2/issue/{issue_key}"
    issue = _jira_get(issue_url, email, token)
    
    linked = []
    for link in issue.get("fields", {}).get("issuelinks", []):
        if "outwardIssue" in link:
            linked.append({"key": link["outwardIssue"]["key"], "direction": "outward"})
        elif "inwardIssue" in link:
            linked.append({"key": link["inwardIssue"]["key"], "direction": "inward"})
    
    result = {
        "key": issue_key,
        "summary": issue.get("fields", {}).get("summary", ""),
        "description": issue.get("fields", {}).get("description", ""),
        "status": issue.get("fields", {}).get("status", {}).get("name", ""),
        "priority": issue.get("fields", {}).get("priority", {}).get("name", ""),
        "issuetype": issue.get("fields", {}).get("issuetype", {}).get("name", ""),
        "linked_issues": linked,
    }
    return result

def _jira_get(url: str, email: str, token: str) -> dict:
    import base64
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python tools/fetch_jira.py ISSUE_KEY")
        print("Example: python tools/fetch_jira.py SLN-12345")
        sys.exit(0)
    result = fetch_jira(sys.argv[1])
    print(json.dumps(result, indent=2))
