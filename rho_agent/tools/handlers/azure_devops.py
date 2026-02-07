"""Azure DevOps handler for work items, comments, and queries."""

import asyncio
import base64
import json
import os
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from ..base import ToolHandler, ToolInvocation, ToolOutput


class AzureDevOpsHandler(ToolHandler):
    """Azure DevOps handler for work item operations.

    Supports searching work items via WIQL, viewing details, adding comments,
    and updating work items. Respects readonly mode for safe operations.
    """

    def __init__(
        self,
        organization: str | None = None,
        pat: str | None = None,
        project: str | None = None,
        readonly: bool = True,
        requires_approval: bool = False,
    ) -> None:
        """Initialize Azure DevOps handler.

        Configuration via constructor args or environment variables:
        - AZURE_DEVOPS_ORG: Organization name
        - AZURE_DEVOPS_PAT: Personal Access Token
        - AZURE_DEVOPS_PROJECT: Default project (optional)
        """
        self._organization = organization or os.environ.get("AZURE_DEVOPS_ORG", "")
        self._pat = pat or os.environ.get("AZURE_DEVOPS_PAT", "")
        self._project = project or os.environ.get("AZURE_DEVOPS_PROJECT", "")
        self._readonly = readonly
        self._requires_approval = requires_approval
        self._api_version = "7.1"

    @property
    def name(self) -> str:
        return "azure_devops"

    @property
    def description(self) -> str:
        org_info = self._organization or "Azure DevOps"
        mode_desc = "read-only" if self._readonly else "full"
        mutation_ops = (
            "'create' (new work item), 'add_comment', 'update', 'link' (connect work items), "
            if not self._readonly
            else ""
        )
        return (
            f"Interact with Azure DevOps work items ({org_info}). "
            f"Operations: 'search' (WIQL query), 'get' (work item by ID), "
            f"'get_comments', 'get_history' (revision history), "
            + mutation_ops
            + f"'list_projects'. Access mode: {mode_desc}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        operations = ["search", "get", "get_comments", "get_history", "list_projects"]
        if not self._readonly:
            operations.extend(["create", "add_comment", "update", "link"])

        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": operations,
                    "description": "The operation to perform",
                },
                "project": {
                    "type": "string",
                    "description": f"Project name (default: {self._project or 'required'})",
                },
                "wiql": {
                    "type": "string",
                    "description": "WIQL query for 'search' operation. Example: SELECT [System.Id], [System.Title] FROM WorkItems WHERE [System.State] = 'Active'",
                },
                "work_item_id": {
                    "type": "integer",
                    "description": "Work item ID for 'get', 'get_comments', 'get_history', 'add_comment', 'update', 'link' operations",
                },
                "work_item_type": {
                    "type": "string",
                    "description": "Work item type for 'create' operation. Common types: Bug, Task, User Story, Feature, Epic",
                },
                "title": {
                    "type": "string",
                    "description": "Title for 'create' operation",
                },
                "comment": {
                    "type": "string",
                    "description": "Comment text for 'add_comment' operation",
                },
                "fields": {
                    "type": "object",
                    "description": 'Fields for \'create\' or \'update\' operation. Example: {"System.State": "Resolved", "System.AssignedTo": "user@example.com"}',
                },
                "target_work_item_id": {
                    "type": "integer",
                    "description": "Target work item ID for 'link' operation",
                },
                "link_type": {
                    "type": "string",
                    "enum": ["parent", "child", "related", "predecessor", "successor"],
                    "description": "Link type for 'link' operation. 'parent'/'child' for hierarchy, 'related' for general links, 'predecessor'/'successor' for dependencies",
                },
                "top": {
                    "type": "integer",
                    "description": "Max results to return for 'search' or 'get_history' (default: 50)",
                },
            },
            "required": ["operation"],
        }

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    def _get_auth_header(self) -> str:
        """Get Basic auth header value from PAT."""
        if not self._pat:
            raise RuntimeError("No Azure DevOps PAT configured. Set AZURE_DEVOPS_PAT env var.")
        # Azure DevOps uses empty username with PAT as password
        credentials = base64.b64encode(f":{self._pat}".encode()).decode()
        return f"Basic {credentials}"

    def _make_request_sync(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        """Make a synchronous HTTP request to Azure DevOps API."""
        if not self._organization:
            raise RuntimeError(
                "No Azure DevOps organization configured. Set AZURE_DEVOPS_ORG env var."
            )

        headers = {
            "Authorization": self._get_auth_header(),
            "Content-Type": content_type,
        }

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        request = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            raise RuntimeError(f"Azure DevOps API error {e.code}: {error_body}")
        except URLError as e:
            raise RuntimeError(f"Azure DevOps connection error: {e.reason}")

    async def _make_request(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        """Make an async HTTP request to Azure DevOps API."""
        return await asyncio.to_thread(self._make_request_sync, method, url, data, content_type)

    def _api_url(
        self,
        path: str,
        project: str | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> str:
        """Build API URL with query parameters."""
        base = f"https://dev.azure.com/{quote(self._organization)}"
        if project:
            base = f"{base}/{quote(project)}"
        params = {"api-version": self._api_version}
        if extra_params:
            params.update(extra_params)
        return f"{base}/_apis/{path}?{urlencode(params)}"

    def _get_project(self, args: dict[str, Any]) -> str:
        """Get project from args or default."""
        project = args.get("project") or self._project
        if not project:
            raise RuntimeError(
                "No project specified. Provide 'project' parameter or set AZURE_DEVOPS_PROJECT."
            )
        return project

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the Azure DevOps operation."""
        args = invocation.arguments
        operation = args.get("operation")

        try:
            if operation == "search":
                return await self._search(args)
            elif operation == "get":
                return await self._get_work_item(args)
            elif operation == "get_comments":
                return await self._get_comments(args)
            elif operation == "get_history":
                return await self._get_history(args)
            elif operation == "create":
                return await self._create_work_item(args)
            elif operation == "add_comment":
                return await self._add_comment(args)
            elif operation == "update":
                return await self._update_work_item(args)
            elif operation == "link":
                return await self._link_work_items(args)
            elif operation == "list_projects":
                return await self._list_projects(args)
            else:
                return ToolOutput(
                    content=f"Unknown operation: {operation}",
                    success=False,
                )
        except Exception as e:
            return ToolOutput(content=str(e), success=False)

    async def _search(self, args: dict[str, Any]) -> ToolOutput:
        """Search work items using WIQL."""
        wiql = args.get("wiql")
        if not wiql:
            return ToolOutput(
                content="Missing 'wiql' parameter for search operation",
                success=False,
            )

        project = self._get_project(args)
        top = args.get("top", 50)

        # Execute WIQL query
        url = self._api_url("wit/wiql", project, {"$top": str(top)})
        result = await self._make_request("POST", url, {"query": wiql})

        work_items = result.get("workItems", [])
        if not work_items:
            return ToolOutput(content="No work items found.", success=True)

        # Fetch work item details (batch request)
        ids = [str(wi["id"]) for wi in work_items[:top]]
        ids_param = ",".join(ids)

        details_url = self._api_url("wit/workitems", project, {"ids": ids_param, "$expand": "None"})
        details = await self._make_request("GET", details_url)

        # Format results
        lines = [f"Found {len(work_items)} work items:\n"]
        for item in details.get("value", []):
            fields = item.get("fields", {})
            wi_id = item.get("id")
            title = fields.get("System.Title", "No title")
            state = fields.get("System.State", "Unknown")
            wi_type = fields.get("System.WorkItemType", "Item")
            assigned = fields.get("System.AssignedTo", {})
            assigned_name = (
                assigned.get("displayName", "Unassigned")
                if isinstance(assigned, dict)
                else "Unassigned"
            )

            lines.append(f"  [{wi_id}] {wi_type}: {title}")
            lines.append(f"      State: {state} | Assigned: {assigned_name}")

        return ToolOutput(content="\n".join(lines), success=True)

    async def _get_work_item(self, args: dict[str, Any]) -> ToolOutput:
        """Get a single work item by ID."""
        wi_id = args.get("work_item_id")
        if not wi_id:
            return ToolOutput(
                content="Missing 'work_item_id' parameter",
                success=False,
            )

        project = self._get_project(args)
        url = self._api_url(f"wit/workitems/{wi_id}", project, {"$expand": "All"})
        item = await self._make_request("GET", url)

        fields = item.get("fields", {})
        lines = [
            f"Work Item #{item.get('id')}",
            f"Type: {fields.get('System.WorkItemType', 'Unknown')}",
            f"Title: {fields.get('System.Title', 'No title')}",
            f"State: {fields.get('System.State', 'Unknown')}",
            f"Assigned To: {fields.get('System.AssignedTo', {}).get('displayName', 'Unassigned') if isinstance(fields.get('System.AssignedTo'), dict) else 'Unassigned'}",
            f"Created: {fields.get('System.CreatedDate', 'Unknown')}",
            f"Area Path: {fields.get('System.AreaPath', 'Unknown')}",
            f"Iteration Path: {fields.get('System.IterationPath', 'Unknown')}",
            "",
            "Description:",
            fields.get("System.Description", "(no description)") or "(no description)",
        ]

        # Include acceptance criteria if present (for User Stories)
        acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
        if acceptance:
            lines.extend(["", "Acceptance Criteria:", acceptance])

        # Include repro steps if present (for Bugs)
        repro = fields.get("Microsoft.VSTS.TCM.ReproSteps")
        if repro:
            lines.extend(["", "Repro Steps:", repro])

        return ToolOutput(content="\n".join(lines), success=True)

    async def _get_comments(self, args: dict[str, Any]) -> ToolOutput:
        """Get comments for a work item."""
        wi_id = args.get("work_item_id")
        if not wi_id:
            return ToolOutput(
                content="Missing 'work_item_id' parameter",
                success=False,
            )

        project = self._get_project(args)
        url = self._api_url(f"wit/workitems/{wi_id}/comments", project)
        result = await self._make_request("GET", url)

        comments = result.get("comments", [])
        if not comments:
            return ToolOutput(content="No comments on this work item.", success=True)

        lines = [f"Comments on Work Item #{wi_id}:\n"]
        for comment in comments:
            author = comment.get("createdBy", {}).get("displayName", "Unknown")
            date = comment.get("createdDate", "Unknown date")
            text = comment.get("text", "(empty)")
            lines.append(f"--- {author} ({date}) ---")
            lines.append(text)
            lines.append("")

        return ToolOutput(content="\n".join(lines), success=True)

    async def _add_comment(self, args: dict[str, Any]) -> ToolOutput:
        """Add a comment to a work item."""
        if self._readonly:
            return ToolOutput(
                content="Cannot add comment in read-only mode",
                success=False,
            )

        wi_id = args.get("work_item_id")
        comment = args.get("comment")

        if not wi_id:
            return ToolOutput(content="Missing 'work_item_id' parameter", success=False)
        if not comment:
            return ToolOutput(content="Missing 'comment' parameter", success=False)

        project = self._get_project(args)
        url = self._api_url(f"wit/workitems/{wi_id}/comments", project)

        result = await self._make_request("POST", url, {"text": comment})

        return ToolOutput(
            content=f"Comment added to work item #{wi_id} (comment ID: {result.get('id')})",
            success=True,
        )

    async def _update_work_item(self, args: dict[str, Any]) -> ToolOutput:
        """Update a work item's fields."""
        if self._readonly:
            return ToolOutput(
                content="Cannot update work item in read-only mode",
                success=False,
            )

        wi_id = args.get("work_item_id")
        fields = args.get("fields")

        if not wi_id:
            return ToolOutput(content="Missing 'work_item_id' parameter", success=False)
        if not fields:
            return ToolOutput(content="Missing 'fields' parameter", success=False)

        project = self._get_project(args)
        url = self._api_url(f"wit/workitems/{wi_id}", project)

        # Azure DevOps uses JSON Patch format for updates
        patch_ops = [
            {"op": "add", "path": f"/fields/{field}", "value": value}
            for field, value in fields.items()
        ]

        result = await self._make_request("PATCH", url, patch_ops, "application/json-patch+json")

        return ToolOutput(
            content=f"Work item #{wi_id} updated successfully. New revision: {result.get('rev')}",
            success=True,
        )

    async def _list_projects(self, args: dict[str, Any]) -> ToolOutput:
        """List all projects in the organization."""
        url = self._api_url("projects")
        result = await self._make_request("GET", url)

        projects = result.get("value", [])
        if not projects:
            return ToolOutput(content="No projects found.", success=True)

        lines = [f"Projects in {self._organization}:\n"]
        for proj in projects:
            name = proj.get("name", "Unknown")
            state = proj.get("state", "Unknown")
            desc = proj.get("description", "")
            lines.append(f"  - {name} ({state})")
            if desc:
                truncated = desc[:60] + "..." if len(desc) > 60 else desc
                lines.append(f"    {truncated}")

        return ToolOutput(content="\n".join(lines), success=True)

    async def _get_history(self, args: dict[str, Any]) -> ToolOutput:
        """Get revision history for a work item."""
        wi_id = args.get("work_item_id")
        if not wi_id:
            return ToolOutput(
                content="Missing 'work_item_id' parameter",
                success=False,
            )

        project = self._get_project(args)
        top = args.get("top", 50)

        # Get all revisions
        url = self._api_url(f"wit/workitems/{wi_id}/revisions", project, {"$top": str(top)})
        result = await self._make_request("GET", url)

        revisions = result.get("value", [])
        if not revisions:
            return ToolOutput(content="No revision history found.", success=True)

        # Track field changes between revisions
        lines = [f"Revision history for Work Item #{wi_id}:\n"]

        # Fields we care about tracking
        tracked_fields = [
            "System.State",
            "System.AssignedTo",
            "System.Title",
            "System.IterationPath",
            "System.AreaPath",
            "System.Reason",
            "Microsoft.VSTS.Common.Priority",
            "Microsoft.VSTS.Common.Severity",
        ]

        prev_fields: dict[str, Any] = {}
        for rev in revisions:
            rev_num = rev.get("rev", 0)
            fields = rev.get("fields", {})

            changed_by = fields.get("System.ChangedBy", {})
            changed_by_name = (
                changed_by.get("displayName", "Unknown")
                if isinstance(changed_by, dict)
                else str(changed_by)
            )
            changed_date = fields.get("System.ChangedDate", "Unknown")

            # Find what changed
            changes = []
            for field in tracked_fields:
                curr_val = fields.get(field)
                prev_val = prev_fields.get(field)

                # Normalize AssignedTo for comparison
                if field == "System.AssignedTo":
                    curr_val = (
                        curr_val.get("displayName") if isinstance(curr_val, dict) else curr_val
                    )
                    prev_val = (
                        prev_val.get("displayName") if isinstance(prev_val, dict) else prev_val
                    )

                if curr_val != prev_val:
                    field_name = field.split(".")[-1]
                    if prev_val is None:
                        changes.append(f"  {field_name}: (none) → {curr_val}")
                    elif curr_val is None:
                        changes.append(f"  {field_name}: {prev_val} → (none)")
                    else:
                        changes.append(f"  {field_name}: {prev_val} → {curr_val}")

            if changes or rev_num == 1:
                lines.append(f"--- Revision {rev_num} ({changed_date}) by {changed_by_name} ---")
                if rev_num == 1:
                    lines.append("  (Created)")
                for change in changes:
                    lines.append(change)
                lines.append("")

            prev_fields = fields.copy()

        return ToolOutput(content="\n".join(lines), success=True)

    async def _create_work_item(self, args: dict[str, Any]) -> ToolOutput:
        """Create a new work item."""
        if self._readonly:
            return ToolOutput(
                content="Cannot create work item in read-only mode",
                success=False,
            )

        wi_type = args.get("work_item_type")
        title = args.get("title")

        if not wi_type:
            return ToolOutput(
                content="Missing 'work_item_type' parameter (e.g., Bug, Task, User Story)",
                success=False,
            )
        if not title:
            return ToolOutput(content="Missing 'title' parameter", success=False)

        project = self._get_project(args)

        # Build JSON Patch document
        patch_ops: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
        ]

        # Add any additional fields
        fields = args.get("fields", {})
        for field, value in fields.items():
            patch_ops.append({"op": "add", "path": f"/fields/{field}", "value": value})

        # URL includes the work item type
        url = self._api_url(f"wit/workitems/${quote(wi_type)}", project)

        result = await self._make_request("POST", url, patch_ops, "application/json-patch+json")

        new_id = result.get("id")
        return ToolOutput(
            content=f"Created {wi_type} #{new_id}: {title}",
            success=True,
            metadata={"work_item_id": new_id},
        )

    async def _link_work_items(self, args: dict[str, Any]) -> ToolOutput:
        """Link two work items together."""
        if self._readonly:
            return ToolOutput(
                content="Cannot link work items in read-only mode",
                success=False,
            )

        wi_id = args.get("work_item_id")
        target_id = args.get("target_work_item_id")
        link_type = args.get("link_type", "related")

        if not wi_id:
            return ToolOutput(content="Missing 'work_item_id' parameter", success=False)
        if not target_id:
            return ToolOutput(content="Missing 'target_work_item_id' parameter", success=False)

        project = self._get_project(args)

        # Map friendly link types to Azure DevOps relation types
        link_type_map = {
            "parent": "System.LinkTypes.Hierarchy-Reverse",
            "child": "System.LinkTypes.Hierarchy-Forward",
            "related": "System.LinkTypes.Related",
            "predecessor": "System.LinkTypes.Dependency-Reverse",
            "successor": "System.LinkTypes.Dependency-Forward",
        }

        relation_type = link_type_map.get(link_type)
        if not relation_type:
            return ToolOutput(
                content=f"Unknown link type: {link_type}. Use: parent, child, related, predecessor, successor",
                success=False,
            )

        # Build the target URL for the related work item
        target_url = f"https://dev.azure.com/{quote(self._organization)}/{quote(project)}/_apis/wit/workitems/{target_id}"

        # JSON Patch to add the relation
        patch_ops: list[dict[str, Any]] = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": relation_type,
                    "url": target_url,
                },
            }
        ]

        url = self._api_url(f"wit/workitems/{wi_id}", project)

        await self._make_request("PATCH", url, patch_ops, "application/json-patch+json")

        return ToolOutput(
            content=f"Linked work item #{wi_id} to #{target_id} as '{link_type}'",
            success=True,
        )
