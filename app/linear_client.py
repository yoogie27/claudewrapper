from __future__ import annotations

import httpx
from typing import Any


class LinearAPIError(RuntimeError):
    """Raised for Linear API errors (expected, no traceback needed)."""
    pass


class LinearClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.base_url = "https://api.linear.app/graphql"
        self.headers = {"Authorization": api_key, "Content-Type": "application/json"}
        # Persistent client — reuses TCP connections across calls
        self._client = httpx.AsyncClient(timeout=30.0, headers=self.headers)
        # Cache: team_id -> {state_name.lower(): state_id}
        self._state_cache: dict[str, dict[str, str]] = {}

    async def __aenter__(self) -> "LinearClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            self.base_url, json={"query": query, "variables": variables}
        )
        if resp.status_code != 200:
            body = resp.text[:500]
            raise LinearAPIError(
                f"Linear API {resp.status_code}: {body}"
            )
        data = resp.json()
        if "errors" in data:
            msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise LinearAPIError(f"Linear GraphQL: {msgs}")
        return data["data"]

    async def get_teams(self) -> list[dict[str, Any]]:
        query = """
        query {
          teams {
            nodes { id name key }
          }
        }
        """
        data = await self._graphql(query, {})
        return data["teams"]["nodes"]

    async def get_issues_updated_since(self, since_iso: str, first: int = 100) -> list[dict[str, Any]]:
        query = """
        query($after: String, $since: DateTimeOrDuration, $first: Int) {
          issues(filter: { updatedAt: { gt: $since } }, first: $first, after: $after, orderBy: updatedAt) {
            nodes {
              id
              identifier
              title
              updatedAt
              createdAt
              state { type name }
              team { id name }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        out: list[dict[str, Any]] = []
        after = None
        while True:
            data = await self._graphql(query, {"after": after, "since": since_iso, "first": first})
            issues = data["issues"]["nodes"]
            out.extend(issues)
            page = data["issues"]["pageInfo"]
            # Stop when we hit the total cap or there are no more pages
            if len(out) >= first or not page["hasNextPage"]:
                break
            after = page["endCursor"]
        return out[:first]

    async def get_issue_comments_since(self, issue_id: str, since_iso: str | None) -> list[dict[str, Any]]:
        query = """
        query($id: String!, $since: DateTimeOrDuration) {
          issue(id: $id) {
            comments(filter: { createdAt: { gt: $since } }, first: 50) {
              nodes { id body createdAt user { id name email } }
            }
          }
        }
        """
        data = await self._graphql(query, {"id": issue_id, "since": since_iso})
        return data["issue"]["comments"]["nodes"]

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        query = """
        query($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            description
            url
            team { id name }
            state { type name }
            labels { nodes { name } }
            assignee { name email }
            comments(first: 50) { nodes { body createdAt user { id name email } } }
          }
        }
        """
        data = await self._graphql(query, {"id": issue_id})
        return data["issue"]

    async def get_issue_by_identifier(self, identifier: str) -> dict[str, Any]:
        query = """
        query($identifier: String!) {
          issue(identifier: $identifier) {
            id
            identifier
            title
            team { id name }
          }
        }
        """
        data = await self._graphql(query, {"identifier": identifier})
        return data["issue"]

    async def get_recent_closed_issues(self, team_id: str, first: int = 20) -> list[dict[str, Any]]:
        query = """
        query($teamId: ID, $first: Int) {
          issues(
            filter: { team: { id: { eq: $teamId } }, state: { type: { eq: "completed" } } },
            first: $first,
            orderBy: updatedAt
          ) {
            nodes { identifier title url updatedAt }
          }
        }
        """
        data = await self._graphql(query, {"teamId": team_id, "first": first})
        return data["issues"]["nodes"]

    async def get_team_issues(
        self,
        team_id: str,
        state_types: list[str] | None = None,
        first: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch issues for a team, optionally filtered by state types."""
        if state_types:
            query = """
            query($teamId: ID, $first: Int, $stateTypes: [String!]) {
              issues(
                filter: { team: { id: { eq: $teamId } }, state: { type: { in: $stateTypes } } },
                first: $first,
                orderBy: updatedAt
              ) {
                nodes {
                  id identifier title url description
                  priority priorityLabel
                  state { type name }
                  team { id name }
                  labels { nodes { name } }
                  assignee { name }
                  updatedAt createdAt
                }
              }
            }
            """
            variables: dict[str, Any] = {"teamId": team_id, "first": first, "stateTypes": state_types}
        else:
            query = """
            query($teamId: ID, $first: Int) {
              issues(
                filter: { team: { id: { eq: $teamId } } },
                first: $first,
                orderBy: updatedAt
              ) {
                nodes {
                  id identifier title url description
                  priority priorityLabel
                  state { type name }
                  team { id name }
                  labels { nodes { name } }
                  assignee { name }
                  updatedAt createdAt
                }
              }
            }
            """
            variables = {"teamId": team_id, "first": first}
        data = await self._graphql(query, variables)
        return data["issues"]["nodes"]

    async def search_issues(self, query_text: str, first: int = 30) -> list[dict[str, Any]]:
        """Full-text search across all issues."""
        query = """
        query($term: String!, $first: Int) {
          searchIssues(term: $term, first: $first) {
            nodes {
              id identifier title url
              priority priorityLabel
              state { type name }
              team { id name }
              labels { nodes { name } }
              assignee { name }
              updatedAt
            }
          }
        }
        """
        data = await self._graphql(query, {"term": query_text, "first": first})
        return data["searchIssues"]["nodes"]

    async def create_comment(self, issue_id: str, body: str) -> str | None:
        """Create a comment on an issue. Returns the comment ID."""
        mutation = """
        mutation($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            success
            comment { id }
          }
        }
        """
        data = await self._graphql(mutation, {"issueId": issue_id, "body": body})
        comment = (data.get("commentCreate") or {}).get("comment")
        return comment["id"] if comment else None

    async def get_workflow_states(self, team_id: str) -> list[dict[str, str]]:
        """Return all workflow states for a team with id, name, type."""
        query = """
        query($teamId: String!) {
          team(id: $teamId) {
            states { nodes { id name type } }
          }
        }
        """
        data = await self._graphql(query, {"teamId": team_id})
        return data["team"]["states"]["nodes"]

    async def get_workflow_state_by_name(self, team_id: str, state_name: str) -> str | None:
        # Populate cache on first call for this team
        if team_id not in self._state_cache:
            states = await self.get_workflow_states(team_id)
            # Store both id and type so callers can update the DB after state transitions
            self._state_cache[team_id] = {
                st["name"].lower(): {"id": st["id"], "type": st["type"]} for st in states
            }
        entry = self._state_cache[team_id].get(state_name.lower())
        return entry["id"] if entry else None

    def get_workflow_state_type(self, team_id: str, state_name: str) -> str | None:
        """Return the Linear state type (e.g. 'started', 'completed') for a state name, if cached."""
        entry = self._state_cache.get(team_id, {}).get(state_name.lower())
        return entry["type"] if entry else None

    async def update_issue_state(self, issue_id: str, state_id: str) -> None:
        mutation = """
        mutation($issueId: String!, $stateId: String!) {
          issueUpdate(id: $issueId, input: { stateId: $stateId }) {
            success
          }
        }
        """
        await self._graphql(mutation, {"issueId": issue_id, "stateId": state_id})
