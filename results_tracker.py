#!/usr/bin/env python3
"""
Agent Results Tracker - GitHub Native
-------------------------------------
This script processes completed agent task results, calculates value generated,
aggregates metrics, and updates reports/dashboards.
It's designed to be run as a GitHub Action daily.

Expected environment variables:
- GITHUB_TOKEN: A GitHub Personal Access Token with repo scope.
"""

import os
import json
import time
import requests
import base64
from datetime import datetime, timezone, timedelta

# --- Configuration Constants ---
GITHUB_API_URL = "https://api.github.com"
OWNER = "zipaJopa"
AGENT_RESULTS_REPO_NAME = "agent-results"
AGENT_RESULTS_REPO_FULL = f"{OWNER}/{AGENT_RESULTS_REPO_NAME}"

OUTPUTS_DIR = "outputs"
PROCESSED_OUTPUTS_DIR = "processed_outputs"
METRICS_DIR = "metrics"
DASHBOARD_FILE = "dashboard.md"

# Value estimation logic (can be expanded)
AGENT_VALUE_ESTIMATES = {
    "github_arbitrage": {"type": "per_item", "value": 125.0},
    "ai_wrapper_factory": {"type": "per_item", "value": 1250.0},
    "saas_template_mill": {"type": "per_item", "value": 6000.0},
    "automation_broker": {"type": "per_item", "value": 2750.0},
    "crypto_degen": {"type": "payload_field", "field": "pnl_usd", "default": 0.0},
    "trade_signal": {"type": "payload_field", "field": "pnl_usd", "default": 0.0}, # Assuming direct P&L
    "influencer_farm": {"type": "payload_field", "field": "revenue_generated_usd", "default": 0.0},
    "course_generator": {"type": "payload_field", "field": "course_sale_value_usd", "default": 0.0},
    "patent_scraper": {"type": "per_item_conditional", "count_field": "valuable_patents_found_count", "value_per_item": 1000.0},
    "domain_flipper": {"type": "payload_field", "field": "profit_usd", "default": 50.0},
    "affiliate_army": {"type": "payload_field", "field": "commission_usd", "default": 0.0},
    "lead_magnet_factory": {"type": "per_item", "value": 200.0},
    "ai_copywriter_swarm": {"type": "per_item", "value": 50.0},
    "price_scraper_network": {"type": "payload_field", "field": "savings_found_usd", "default": 0.0},
    "startup_idea_generator": {"type": "per_item_conditional", "count_field": "validated_ideas_count", "value_per_item": 100.0},
    "harvest": {"type": "count_only"}, # Provides leads, value captured by downstream agents
    "self_healing": {"type": "count_only"}, # Value is operational stability, harder to quantify directly here
    "performance_optimization": {"type": "count_only"}, # Value is cost saving or improved UX
    "financial_management": {"type": "count_only"}, # Tracks internal finances or user finances
    # Default for unknown task types or those not explicitly listed
    "unknown_type_default": {"type": "count_only"},
}


# --- GitHub Interaction Helper Class (Consistent with other constellation scripts) ---
class GitHubInteraction:
    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _request(self, method, endpoint, data=None, params=None, max_retries=3, base_url=GITHUB_API_URL):
        url = f"{base_url}{endpoint}"
        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, params=params, json=data)
                if 'X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) < 10:
                    reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                    sleep_duration = max(0, reset_time - time.time()) + 5
                    print(f"Rate limit low. Sleeping for {sleep_duration:.2f} seconds.")
                    time.sleep(sleep_duration)
                response.raise_for_status()
                return response.json() if response.content else {}
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 403 and "rate limit exceeded" in e.response.text.lower():
                    reset_time = int(e.response.headers.get('X-RateLimit-Reset', time.time() + 60 * (attempt + 1)))
                    sleep_duration = max(0, reset_time - time.time()) + 5
                    print(f"Rate limit exceeded. Retrying in {sleep_duration:.2f}s (attempt {attempt+1}/{max_retries}).")
                    time.sleep(sleep_duration)
                    continue
                elif e.response.status_code == 404 and method == "GET":
                    return None
                elif e.response.status_code == 422 and "No commit found for SHA" in e.response.text:
                     return {"error": "file_not_found_for_update", "sha": None}
                print(f"GitHub API request failed ({method} {url}): {e.response.status_code} - {e.response.text}")
                if attempt == max_retries - 1:
                    raise
            except requests.exceptions.RequestException as e:
                print(f"GitHub API request failed ({method} {url}): {e}")
                if attempt == max_retries - 1:
                    raise
            time.sleep(2 ** attempt)
        return {}

    def list_files(self, repo_full_name, path):
        endpoint = f"/repos/{repo_full_name}/contents/{path}"
        response_data = self._request("GET", endpoint)
        if response_data and isinstance(response_data, list):
            return [item for item in response_data if item.get("type") == "file"]
        return []

    def get_file_content_and_sha(self, repo_full_name, file_path):
        endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        file_data = self._request("GET", endpoint)
        if file_data and "content" in file_data and "sha" in file_data:
            content = base64.b64decode(file_data["content"]).decode('utf-8')
            return content, file_data["sha"]
        return None, None

    def create_or_update_file(self, repo_full_name, file_path, content_str, commit_message, current_sha=None, branch="main"):
        encoded_content = base64.b64encode(content_str.encode('utf-8')).decode('utf-8')
        payload = {"message": commit_message, "content": encoded_content, "branch": branch}
        if current_sha:
            payload["sha"] = current_sha
        endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        response = self._request("PUT", endpoint, data=payload)
        return response and "content" in response and "sha" in response["content"]

    def delete_file(self, repo_full_name, file_path, sha, commit_message, branch="main"):
        payload = {"message": commit_message, "sha": sha, "branch": branch}
        endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        response = self._request("DELETE", endpoint, data=payload)
        return response is not None


# --- Results Tracking Logic ---
class ResultsTracker:
    def __init__(self, github_token):
        self.gh = GitHubInteraction(github_token)
        self.today_date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.daily_metrics_path = f"{METRICS_DIR}/daily_metrics_{self.today_date_str}.json"

    def _load_daily_metrics(self):
        print(f"Loading existing daily metrics from {self.daily_metrics_path}...")
        content_str, sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path)
        if content_str:
            try:
                metrics = json.loads(content_str)
                print("Existing metrics loaded.")
                return metrics, sha
            except json.JSONDecodeError:
                print(f"Error parsing existing metrics file. Initializing new metrics.")
        
        print("No existing metrics file found or file was invalid. Initializing new metrics for today.")
        return {
            "date": self.today_date_str,
            "total_value_generated_usd": 0.0,
            "tasks_with_results_processed_count": 0, # Tasks whose results were successfully processed by this tracker
            "value_by_agent_type": {},
            "detailed_value_breakdown": [], # List of {task_id, agent_type, value_usd, description}
            "processed_result_file_shas": [] # List of SHAs of result files already included
        }, None

    def _save_daily_metrics(self, metrics_data, current_sha):
        print(f"Saving daily metrics to {self.daily_metrics_path}...")
        content_str = json.dumps(metrics_data, indent=2)
        commit_message = f"Update daily metrics and value report for {self.today_date_str}"
        return self.gh.create_or_update_file(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path, content_str, commit_message, current_sha)

    def _calculate_task_value(self, task_type, result_payload):
        value_config = AGENT_VALUE_ESTIMATES.get(task_type, AGENT_VALUE_ESTIMATES["unknown_type_default"])
        task_value = 0.0
        description = f"Task type: {task_type}"

        if value_config["type"] == "per_item":
            # Assumes the existence of the result file means one successful item
            task_value = value_config["value"]
            description = f"Completed {task_type} task."
        elif value_config["type"] == "payload_field":
            task_value = result_payload.get(value_config["field"], value_config["default"])
            description = f"{task_type} result: {value_config['field']} = {task_value:.2f} USD"
        elif value_config["type"] == "per_item_conditional":
            item_count = result_payload.get(value_config["count_field"], 0)
            task_value = item_count * value_config["value_per_item"]
            description = f"{task_type} found {item_count} items, value {task_value:.2f} USD."
        
        # Ensure task_value is float
        try:
            task_value = float(task_value)
        except (ValueError, TypeError):
            print(f"Warning: Could not convert value '{task_value}' to float for task type {task_type}. Defaulting to 0.")
            task_value = 0.0

        return task_value, description

    def _archive_processed_file(self, file_info, daily_results_path):
        source_path = file_info["path"]
        file_name = file_info["name"]
        source_sha = file_info["sha"]
        
        archive_dir = f"{PROCESSED_OUTPUTS_DIR}/{self.today_date_str}"
        archive_path = f"{archive_dir}/{file_name}"
        print(f"Archiving {source_path} to {archive_path}...")

        content_str, _ = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, source_path)
        if content_str is None:
            print(f"Error: Could not get content of {source_path} for archiving. Skipping archival.")
            return False

        # Create/Update file in archive
        commit_archive_msg = f"Archive processed result file: {file_name} for {self.today_date_str}"
        _, archive_sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, archive_path) # Get SHA if exists
        if not self.gh.create_or_update_file(AGENT_RESULTS_REPO_FULL, archive_path, content_str, commit_archive_msg, archive_sha):
            print(f"Error: Failed to write {archive_path}. Archival failed.")
            return False
        
        # Delete original file
        commit_delete_msg = f"Delete processed result file: {file_name} from {daily_results_path}"
        if not self.gh.delete_file(AGENT_RESULTS_REPO_FULL, source_path, source_sha, commit_delete_msg):
            print(f"Error: Failed to delete {source_path} after archiving. Manual cleanup may be needed.")
            # This is a non-ideal state, but the SHA tracking should prevent re-processing value.
            return False
        
        print(f"Successfully archived {file_name}.")
        return True

    def process_daily_results(self):
        print(f"\n--- Starting Results Processing for {self.today_date_str} ---")
        daily_metrics, metrics_file_sha = self._load_daily_metrics()
        
        daily_results_path = f"{OUTPUTS_DIR}/{self.today_date_str}"
        result_files = self.gh.list_files(AGENT_RESULTS_REPO_FULL, daily_results_path)

        if not result_files:
            print(f"No result files found in {daily_results_path} for today.")
            # Still save metrics if it's newly initialized or if controller might have updated it
            if metrics_file_sha is None: # If it was newly initialized
                 self._save_daily_metrics(daily_metrics, metrics_file_sha)
            self.generate_dashboard_markdown(daily_metrics) # Generate dashboard even if no new results
            print("--- Results Processing Finished (No new files) ---")
            return

        print(f"Found {len(result_files)} result files in {daily_results_path}.")
        new_files_processed_this_run = 0

        for file_info in result_files:
            file_path = file_info["path"]
            file_sha = file_info["sha"]

            if file_sha in daily_metrics["processed_result_file_shas"]:
                # print(f"Skipping already processed file: {file_path} (SHA: {file_sha})")
                continue
            
            print(f"Processing new result file: {file_path} (SHA: {file_sha})")
            content_str, _ = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, file_path)
            if not content_str:
                print(f"Error: Could not get content for {file_path}. Skipping.")
                continue

            try:
                result_data = json.loads(content_str)
                # Assuming result_data directly contains the payload from the agent
                # And that task_id and task_type are in the filename or metadata if not in payload
                # Example filename: outputs/YYYY-MM-DD/TASKTYPE_TASKID.json
                file_name_parts = file_info["name"].replace(".json", "").split("_", 1)
                task_type_from_filename = file_name_parts[0]
                task_id_from_filename = file_name_parts[1] if len(file_name_parts) > 1 else "unknown_task_id"

                # Prefer task_type from payload if available, else from filename
                task_type = result_data.get("task_type", task_type_from_filename)
                task_id = result_data.get("task_id", task_id_from_filename) 
                
                # The actual result payload might be nested, e.g. under a 'result' key or be the root
                task_payload = result_data.get("result", result_data) if isinstance(result_data.get("result"), dict) else result_data


                value_usd, value_desc = self._calculate_task_value(task_type, task_payload)

                daily_metrics["tasks_with_results_processed_count"] += 1
                daily_metrics["total_value_generated_usd"] += value_usd
                
                if task_type not in daily_metrics["value_by_agent_type"]:
                    daily_metrics["value_by_agent_type"][task_type] = 0.0
                daily_metrics["value_by_agent_type"][task_type] += value_usd
                
                if value_usd > 0: # Only log detailed breakdown for tasks that generated value
                    daily_metrics["detailed_value_breakdown"].append({
                        "task_id": task_id,
                        "file_path": file_path,
                        "agent_type": task_type,
                        "value_usd": value_usd,
                        "description": value_desc,
                        "processed_at": datetime.now(timezone.utc).isoformat()
                    })
                
                daily_metrics["processed_result_file_shas"].append(file_sha)
                new_files_processed_this_run += 1
                
                # Archive the file after successful processing of its content
                self._archive_processed_file(file_info, daily_results_path)

            except json.JSONDecodeError:
                print(f"Error parsing JSON from result file {file_path}. Skipping.")
            except Exception as e:
                print(f"Unexpected error processing file {file_path}: {e}")
                import traceback
                traceback.print_exc()
        
        if new_files_processed_this_run > 0:
            if self._save_daily_metrics(daily_metrics, metrics_file_sha):
                 metrics_file_sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path)[1] # refresh sha
            else:
                print("CRITICAL: Failed to save updated daily metrics after processing new files.")
        else:
            print("No new result files were processed in this run.")

        self.generate_dashboard_markdown(daily_metrics, metrics_file_sha) # Pass current metrics_file_sha for dashboard commit
        print(f"--- Results Processing Finished for {self.today_date_str} ---")

    def generate_dashboard_markdown(self, metrics_data, metrics_file_sha):
        print(f"Generating dashboard markdown ({DASHBOARD_FILE})...")
        
        dashboard_content = [f"# AI Constellation Performance Dashboard - {metrics_data['date']}\n\n"]
        dashboard_content.append(f"## Daily Summary ({metrics_data['date']})\n")
        dashboard_content.append(f"- **Total Value Generated Today:** ${metrics_data['total_value_generated_usd']:.2f} USD\n")
        dashboard_content.append(f"- **Tasks with Results Processed:** {metrics_data['tasks_with_results_processed_count']}\n")
        
        dashboard_content.append("\n### Value Breakdown by Agent Type (Today):\n")
        if metrics_data["value_by_agent_type"]:
            dashboard_content.append("| Agent Type | Value Generated (USD) |\n")
            dashboard_content.append("|------------|-----------------------|\n")
            for agent_type, value in sorted(metrics_data["value_by_agent_type"].items(), key=lambda item: item[1], reverse=True):
                dashboard_content.append(f"| {agent_type} | ${value:.2f} |\n")
        else:
            dashboard_content.append("No value generated by specific agent types today.\n")

        dashboard_content.append("\n### Notable Value Events (Today):\n")
        if metrics_data["detailed_value_breakdown"]:
            # Sort by value, descending, take top 10
            top_events = sorted(metrics_data["detailed_value_breakdown"], key=lambda x: x.get("value_usd", 0.0), reverse=True)[:10]
            for event in top_events:
                dashboard_content.append(f"- **Task {event['task_id']} ({event['agent_type']}):** ${event['value_usd']:.2f} USD - {event['description']}\n")
        else:
            dashboard_content.append("No specific high-value events logged today.\n")
            
        dashboard_content.append(f"\n---\n*Last updated: {datetime.now(timezone.utc).isoformat()}*\n")
        dashboard_content.append(f"*View detailed daily metrics [here](./{self.daily_metrics_path})*\n")

        # Get current SHA of dashboard.md to update it
        _, dashboard_sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, DASHBOARD_FILE)
        
        commit_message = f"Update dashboard for {metrics_data['date']}"
        if self.gh.create_or_update_file(AGENT_RESULTS_REPO_FULL, DASHBOARD_FILE, "".join(dashboard_content), commit_message, dashboard_sha):
            print("Dashboard markdown updated successfully.")
        else:
            print("Error updating dashboard markdown.")

    def run(self):
        self.process_daily_results()

if __name__ == "__main__":
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        print("Error: GITHUB_TOKEN environment variable not set.")
    else:
        tracker = ResultsTracker(github_token)
        tracker.run()
