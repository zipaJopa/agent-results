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
DASHBOARD_FILE = "CONSTELLATION_STATUS.md" # Matching the self_test.py output file

# Value estimation logic (can be expanded)
# For crypto agents, 'pnl_usdt' field in their result payload is prioritized.
AGENT_VALUE_ESTIMATES = {
    # Crypto Agents (prioritize pnl_usdt from payload)
    "crypto-trading-agent": {"type": "payload_field", "field": "pnl_usdt", "default": 0.0, "value_category": "crypto"},
    "memecoin-detector-agent": {"type": "payload_field", "field": "pnl_usdt", "default": 0.0, "value_category": "crypto"},
    "defi-yield-farming-agent": {"type": "payload_field", "field": "pnl_usdt", "default": 0.0, "value_category": "crypto"},
    "pionex-trader-usdt-v1": {"type": "payload_field", "field": "pnl_usdt", "default": 0.0, "value_category": "crypto"}, # From deploy_trading_agent.py

    # Fiat/Estimated Value Agents
    "github_arbitrage": {"type": "per_item", "value": 125.0, "value_category": "fiat"},
    "github-arbitrage-agent": {"type": "payload_field", "field": "value_score", "default": 0.0, "value_category": "fiat"}, # From metaconstellation_core.py
    "ai_wrapper_factory": {"type": "per_item", "value": 1250.0, "value_category": "fiat"},
    "api-wrapper-factory-agent": {"type": "payload_field", "field": "value_score", "default": 0.0, "value_category": "fiat"}, # From metaconstellation_core.py
    "saas_template_mill": {"type": "per_item", "value": 6000.0, "value_category": "fiat"},
    "automation_broker": {"type": "per_item", "value": 2750.0, "value_category": "fiat"},
    "influencer_farm": {"type": "payload_field", "field": "revenue_generated_usd", "default": 0.0, "value_category": "fiat"},
    "course_generator": {"type": "payload_field", "field": "course_sale_value_usd", "default": 0.0, "value_category": "fiat"},
    "patent_scraper": {"type": "per_item_conditional", "count_field": "valuable_patents_found_count", "value_per_item": 1000.0, "value_category": "fiat"},
    "domain_flipper": {"type": "payload_field", "field": "profit_usd", "default": 50.0, "value_category": "fiat"},
    "affiliate_army": {"type": "payload_field", "field": "commission_usd", "default": 0.0, "value_category": "fiat"},
    "lead_magnet_factory": {"type": "per_item", "value": 200.0, "value_category": "fiat"},
    "ai_copywriter_swarm": {"type": "per_item", "value": 50.0, "value_category": "fiat"},
    "content-generation-agent": {"type": "payload_field", "field": "estimated_value_usd", "default": 10.0, "value_category": "fiat"}, # From metaconstellation_core.py
    "price_scraper_network": {"type": "payload_field", "field": "savings_found_usd", "default": 0.0, "value_category": "fiat"},
    "startup_idea_generator": {"type": "per_item_conditional", "count_field": "validated_ideas_count", "value_per_item": 100.0, "value_category": "fiat"},
    
    # Operational/Count-Only Agents (value captured downstream or operational)
    "harvest": {"type": "count_only", "value_category": "operational"},
    "self_healing": {"type": "count_only", "value_category": "operational"},
    "performance_optimization": {"type": "count_only", "value_category": "operational"},
    "financial_management": {"type": "count_only", "value_category": "operational"},
    
    # Default for unknown task types
    "unknown_type_default": {"type": "count_only", "value_category": "unknown"},
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
                elif e.response.status_code == 404 and method == "GET": # File not found is okay for checks
                    return None
                elif e.response.status_code == 422 and "No commit found for SHA" in e.response.text: # Trying to update a non-existent file with SHA
                     return {"error": "file_not_found_for_update", "sha": None}
                print(f"GitHub API request failed ({method} {url}): {e.response.status_code} - {e.response.text}")
                if attempt == max_retries - 1:
                    raise
            except requests.exceptions.RequestException as e:
                print(f"GitHub API request failed ({method} {url}): {e}")
                if attempt == max_retries - 1:
                    raise
            time.sleep(2 ** attempt) # Exponential backoff
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
        if current_sha: # If SHA is provided, it's an update
            payload["sha"] = current_sha
        
        endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        response = self._request("PUT", endpoint, data=payload)
        return response and "content" in response and "sha" in response["content"]

    def delete_file(self, repo_full_name, file_path, sha, commit_message, branch="main"):
        payload = {"message": commit_message, "sha": sha, "branch": branch}
        endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        response = self._request("DELETE", endpoint, data=payload)
        return response is not None # DELETE returns 204 No Content on success, response is {}


# --- Results Tracking Logic ---
class ResultsTracker:
    def __init__(self, github_token):
        self.gh = GitHubInteraction(github_token)
        self.today_date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.daily_metrics_path = f"{METRICS_DIR}/daily_metrics_{self.today_date_str}.json"

    def _load_daily_metrics(self):
        print(f"Loading existing daily metrics from {self.daily_metrics_path}...")
        content_str, sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path)
        
        default_metrics = {
            "date": self.today_date_str,
            "grand_total_value_usd": 0.0,
            "total_crypto_pnl_usd": 0.0,
            "total_fiat_value_usd": 0.0,
            "crypto_trades_count": 0,
            "fiat_tasks_count": 0,
            "operational_tasks_count": 0,
            "pnl_by_crypto_agent": {}, # {agent_type: total_pnl}
            "value_by_fiat_agent": {}, # {agent_type: total_value}
            "tasks_by_operational_agent": {}, # {agent_type: count}
            "detailed_value_breakdown": [], # List of {task_id, agent_type, value_usd, value_category, description, file_path, processed_at}
            "processed_result_file_shas": [], # List of SHAs of result files already included
            "errors_processing_results": [] # List of {file_path, error_message, timestamp}
        }

        if content_str:
            try:
                metrics = json.loads(content_str)
                # Ensure all default keys are present
                for key, default_value in default_metrics.items():
                    if key not in metrics:
                        metrics[key] = default_value
                print("Existing metrics loaded and validated.")
                return metrics, sha
            except json.JSONDecodeError:
                print(f"Error parsing existing metrics file. Initializing new metrics.")
        
        print("No existing metrics file found or file was invalid. Initializing new metrics for today.")
        return default_metrics, None

    def _save_daily_metrics(self, metrics_data, current_sha):
        print(f"Saving daily metrics to {self.daily_metrics_path}...")
        content_str = json.dumps(metrics_data, indent=2)
        commit_message = f"Update daily metrics and value report for {self.today_date_str}"
        return self.gh.create_or_update_file(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path, content_str, commit_message, current_sha)

    def _calculate_task_value(self, task_type, result_payload):
        value_config = AGENT_VALUE_ESTIMATES.get(task_type, AGENT_VALUE_ESTIMATES["unknown_type_default"])
        task_value = 0.0
        value_category = value_config.get("value_category", "unknown")
        description = f"Task type: {task_type}"

        # Prioritize pnl_usdt if present, especially for crypto agents
        if "pnl_usdt" in result_payload:
            try:
                task_value = float(result_payload["pnl_usdt"])
                value_category = "crypto" # Override category if pnl_usdt is found
                description = f"{task_type} P&L: {task_value:.2f} USD"
                return task_value, value_category, description
            except (ValueError, TypeError):
                print(f"Warning: Could not convert pnl_usdt '{result_payload['pnl_usdt']}' to float for {task_type}. Using configured method.")
        
        # Fallback to configured value calculation
        if value_config["type"] == "per_item":
            task_value = value_config["value"]
            description = f"Completed {task_type} task."
        elif value_config["type"] == "payload_field":
            task_value = result_payload.get(value_config["field"], value_config.get("default", 0.0))
            description = f"{task_type} result: {value_config['field']} = {task_value}" # Value might not be USD here
            if isinstance(task_value, (int, float)):
                 description += " USD" if "usd" in value_config['field'].lower() or value_category == "fiat" else ""
        elif value_config["type"] == "per_item_conditional":
            item_count = result_payload.get(value_config["count_field"], 0)
            task_value = item_count * value_config["value_per_item"]
            description = f"{task_type} found {item_count} items, value {task_value:.2f} USD."
        elif value_config["type"] == "count_only":
            task_value = 0.0 # No direct monetary value, just counted
            description = f"Processed {task_type} task (operational)."
        
        # Ensure task_value is float
        try:
            task_value = float(task_value)
        except (ValueError, TypeError):
            print(f"Warning: Could not convert final value '{task_value}' to float for task type {task_type}. Defaulting to 0.")
            task_value = 0.0

        return task_value, value_category, description

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
            return False # Non-ideal state, but SHA tracking prevents re-processing value.
        
        print(f"Successfully archived {file_name}.")
        return True

    def process_daily_results(self):
        print(f"\n--- Starting Results Processing for {self.today_date_str} ---")
        daily_metrics, metrics_file_sha = self._load_daily_metrics()
        
        daily_results_path = f"{OUTPUTS_DIR}/{self.today_date_str}"
        result_files = self.gh.list_files(AGENT_RESULTS_REPO_FULL, daily_results_path)

        if not result_files:
            print(f"No result files found in {daily_results_path} for today.")
            if metrics_file_sha is None: # If it was newly initialized
                 self._save_daily_metrics(daily_metrics, metrics_file_sha)
            self.generate_dashboard_markdown(daily_metrics)
            print("--- Results Processing Finished (No new files) ---")
            return

        print(f"Found {len(result_files)} result files in {daily_results_path}.")
        new_files_processed_this_run = 0

        for file_info in result_files:
            file_path = file_info["path"]
            file_sha = file_info["sha"]

            if file_sha in daily_metrics["processed_result_file_shas"]:
                continue # Skip already processed file
            
            print(f"Processing new result file: {file_path} (SHA: {file_sha})")
            content_str, _ = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, file_path)
            if not content_str:
                err_msg = f"Could not get content for {file_path}"
                print(f"Error: {err_msg}. Skipping.")
                daily_metrics["errors_processing_results"].append({"file_path": file_path, "error": err_msg, "timestamp": datetime.now(timezone.utc).isoformat()})
                continue

            try:
                result_data = json.loads(content_str)
                file_name_parts = file_info["name"].replace(".json", "").split("_", 1) # Assumes AGENTTYPE_TASKID_TIMESTAMP.json or similar
                task_type_from_filename = file_name_parts[0]
                task_id_from_filename = file_name_parts[1] if len(file_name_parts) > 1 else f"unknown_task_id_{file_info['name']}"

                task_type = result_data.get("agent_type", result_data.get("task_type", task_type_from_filename))
                task_id = result_data.get("task_id", task_id_from_filename) 
                
                task_payload = result_data.get("result", result_data) if isinstance(result_data.get("result"), dict) else result_data

                value_usd, value_category, value_desc = self._calculate_task_value(task_type, task_payload)

                if value_category == "crypto":
                    daily_metrics["crypto_trades_count"] += 1
                    daily_metrics["total_crypto_pnl_usd"] += value_usd
                    daily_metrics["pnl_by_crypto_agent"].setdefault(task_type, 0.0)
                    daily_metrics["pnl_by_crypto_agent"][task_type] += value_usd
                elif value_category == "fiat":
                    daily_metrics["fiat_tasks_count"] += 1
                    daily_metrics["total_fiat_value_usd"] += value_usd
                    daily_metrics["value_by_fiat_agent"].setdefault(task_type, 0.0)
                    daily_metrics["value_by_fiat_agent"][task_type] += value_usd
                elif value_category == "operational":
                    daily_metrics["operational_tasks_count"] += 1
                    daily_metrics["tasks_by_operational_agent"].setdefault(task_type, 0)
                    daily_metrics["tasks_by_operational_agent"][task_type] += 1
                
                daily_metrics["grand_total_value_usd"] = daily_metrics["total_crypto_pnl_usd"] + daily_metrics["total_fiat_value_usd"]
                
                # Log all processed tasks, even with zero value, for completeness
                daily_metrics["detailed_value_breakdown"].append({
                    "task_id": task_id,
                    "file_path": file_path,
                    "agent_type": task_type,
                    "value_usd": value_usd,
                    "value_category": value_category,
                    "description": value_desc,
                    "processed_at": datetime.now(timezone.utc).isoformat()
                })
                
                daily_metrics["processed_result_file_shas"].append(file_sha)
                new_files_processed_this_run += 1
                
                self._archive_processed_file(file_info, daily_results_path)

            except json.JSONDecodeError:
                err_msg = f"Error parsing JSON from result file {file_path}"
                print(f"Error: {err_msg}. Skipping.")
                daily_metrics["errors_processing_results"].append({"file_path": file_path, "error": err_msg, "timestamp": datetime.now(timezone.utc).isoformat()})
            except Exception as e:
                err_msg = f"Unexpected error processing file {file_path}: {str(e)}"
                print(f"Error: {err_msg}")
                daily_metrics["errors_processing_results"].append({"file_path": file_path, "error": err_msg, "timestamp": datetime.now(timezone.utc).isoformat()})
                import traceback
                traceback.print_exc()
        
        if new_files_processed_this_run > 0 or daily_metrics["errors_processing_results"]:
            if self._save_daily_metrics(daily_metrics, metrics_file_sha):
                 # Get the new SHA after saving, important if dashboard commit relies on this SHA
                 _, metrics_file_sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, self.daily_metrics_path)
            else:
                print("CRITICAL: Failed to save updated daily metrics after processing new files.")
        else:
            print("No new result files were processed in this run.")

        self.generate_dashboard_markdown(daily_metrics)
        print(f"--- Results Processing Finished for {self.today_date_str} ---")

    def generate_dashboard_markdown(self, metrics_data):
        print(f"Generating dashboard markdown ({DASHBOARD_FILE})...")
        
        dashboard_content = [f"# AI Constellation Performance Dashboard - {metrics_data['date']}\n\n"]
        dashboard_content.append(f"## Overall Summary ({metrics_data['date']})\n")
        dashboard_content.append(f"- **Grand Total Value Generated (Crypto P&L + Fiat Value):** ${metrics_data['grand_total_value_usd']:.2f} USD\n")
        dashboard_content.append(f"- **Total Crypto P&L:** ${metrics_data['total_crypto_pnl_usd']:.2f} USD from {metrics_data['crypto_trades_count']} trades/results\n")
        dashboard_content.append(f"- **Total Estimated Fiat Value:** ${metrics_data['total_fiat_value_usd']:.2f} USD from {metrics_data['fiat_tasks_count']} tasks\n")
        dashboard_content.append(f"- **Operational Tasks Processed:** {metrics_data['operational_tasks_count']}\n")
        
        dashboard_content.append("\n### Crypto P&L Breakdown by Agent (Today):\n")
        if metrics_data["pnl_by_crypto_agent"]:
            dashboard_content.append("| Agent Type | P&L (USD) |\n")
            dashboard_content.append("|------------|-----------|\n")
            for agent_type, pnl in sorted(metrics_data["pnl_by_crypto_agent"].items(), key=lambda item: item[1], reverse=True):
                dashboard_content.append(f"| {agent_type} | ${pnl:.2f} |\n")
        else:
            dashboard_content.append("No crypto P&L recorded today.\n")

        dashboard_content.append("\n### Fiat Value Breakdown by Agent (Today):\n")
        if metrics_data["value_by_fiat_agent"]:
            dashboard_content.append("| Agent Type | Value Generated (USD) |\n")
            dashboard_content.append("|------------|-----------------------|\n")
            for agent_type, value in sorted(metrics_data["value_by_fiat_agent"].items(), key=lambda item: item[1], reverse=True):
                dashboard_content.append(f"| {agent_type} | ${value:.2f} |\n")
        else:
            dashboard_content.append("No fiat value generated by specific agent types today.\n")

        dashboard_content.append("\n### Top Value Events (Today - Crypto & Fiat):\n")
        if metrics_data["detailed_value_breakdown"]:
            top_events = sorted(
                [event for event in metrics_data["detailed_value_breakdown"] if event.get("value_usd", 0.0) != 0], 
                key=lambda x: abs(x.get("value_usd", 0.0)), 
                reverse=True
            )[:15] # Show top 15 events with non-zero value
            if top_events:
                for event in top_events:
                    category_tag = f"[{event['value_category'].upper()}] " if event['value_category'] != 'unknown' else ""
                    dashboard_content.append(f"- **{category_tag}{event['agent_type']} (Task {event.get('task_id', 'N/A')}):** ${event['value_usd']:.2f} USD - {event['description']}\n")
            else:
                dashboard_content.append("No specific value-generating events logged today.\n")
        else:
            dashboard_content.append("No detailed value events logged today.\n")

        if metrics_data["errors_processing_results"]:
            dashboard_content.append("\n### Errors During Result Processing:\n")
            for err in metrics_data["errors_processing_results"][:10]: # Show up to 10 errors
                dashboard_content.append(f"- **File:** `{err['file_path']}` - **Error:** {err['error']} (at {err['timestamp']})\n")
            if len(metrics_data["errors_processing_results"]) > 10:
                dashboard_content.append(f"- ...and {len(metrics_data['errors_processing_results']) - 10} more errors.\n")
            
        dashboard_content.append(f"\n---\n*Last updated: {datetime.now(timezone.utc).isoformat()}*\n")
        dashboard_content.append(f"*View detailed daily metrics JSON [here](./{self.daily_metrics_path})*\n")
        dashboard_content.append(f"*This dashboard is updated by the `results_tracker.py` script in the `agent-results` repository.*")


        _, dashboard_sha = self.gh.get_file_content_and_sha(AGENT_RESULTS_REPO_FULL, DASHBOARD_FILE)
        
        commit_message = f"Update performance dashboard for {metrics_data['date']}"
        if self.gh.create_or_update_file(AGENT_RESULTS_REPO_FULL, DASHBOARD_FILE, "".join(dashboard_content), commit_message, dashboard_sha):
            print("Dashboard markdown updated successfully.")
        else:
            print("Error updating dashboard markdown.")

    def run(self):
        self.process_daily_results()

if __name__ == "__main__":
    github_token = os.getenv("GH_PAT") # Ensure GH_PAT is used, not GITHUB_TOKEN for cross-repo
    if not github_token:
        print("Error: GH_PAT environment variable not set.")
        print("Please ensure GH_PAT (GitHub Personal Access Token with repo scope) is available to the script.")
    else:
        tracker = ResultsTracker(github_token)
        tracker.run()
