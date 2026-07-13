import os
import sys
import json
import csv
from datetime import datetime, timedelta
from azure.identity import DefaultAzureCredential, DeviceCodeCredential
from azure.mgmt.storage import StorageManagementClient
from azure.monitor.querymetrics import MetricsClient, MetricAggregationType

# Force UTF-8 encoding for stdout and stderr to prevent garbled text (亂碼) on Windows console redirect
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Configuration files
CONFIG_FILE = "config.json"
CSV_FILE = "storage_inventory.csv"
MD_FILE = "storage_inventory.md"
VERSION = "1.1.0"

def load_config():
    """Load subscriptions from config.json."""
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found. Please create it using config.json.template.")
        exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def get_credential():
    """Retrieve Azure credentials using DefaultAzureCredential with a DeviceCode fallback."""
    print("Authenticating with Azure...", flush=True)
    try:
        # First, try DefaultAzureCredential
        credential = DefaultAzureCredential(exclude_shared_token_cache_credential=True)
        # Test if credential can fetch a token (fails fast if not authenticated)
        credential.get_token("https://management.azure.com/.default")
        print("Successfully authenticated using DefaultAzureCredential.", flush=True)
        return credential
    except Exception as e:
        print(f"\nDefaultAzureCredential failed to authenticate: {e}", flush=True)
        print("Attempting Device Code login fallback...", flush=True)
        try:
            def callback(verification_uri, user_code, expires_on):
                print(f"\n========================================================", flush=True)
                print(f"To sign in, use a web browser to open the page:", flush=True)
                print(f"  {verification_uri}", flush=True)
                print(f"And enter the code to authenticate:", flush=True)
                print(f"  {user_code}", flush=True)
                print(f"========================================================\n", flush=True)

            # DeviceCodeCredential prompts on stdout/stderr, which will show up in our logs
            credential = DeviceCodeCredential(prompt_callback=callback)
            # Test if credential can fetch a token (triggers the console prompt)
            credential.get_token("https://management.azure.com/.default")
            print("Successfully authenticated via Device Code.", flush=True)
            return credential
        except Exception as auth_err:
            print(f"Device Code authentication failed: {auth_err}", flush=True)
            print("\nPlease make sure you are logged in using Azure CLI ('az login') or Azure PowerShell ('Connect-AzAccount').", flush=True)
            exit(1)

def query_metrics(metrics_client, resource_id):
    """Query capacity, transactions, ingress, and egress metrics for the last 30 days."""
    capacity_gb = 0.0
    transactions = 0
    ingress_gb = 0.0
    egress_gb = 0.0
    
    try:
        # Query metrics for the last 30 days
        timespan = timedelta(days=30)
        granularity = timedelta(hours=1) # Hourly data points (PT1H) - commonly supported grain
        
        response = metrics_client.query_resources(
            resource_ids=[resource_id],
            metric_namespace="Microsoft.Storage/storageAccounts",
            metric_names=["UsedCapacity", "Transactions", "Ingress", "Egress"],
            timespan=timespan,
            granularity=granularity,
            aggregations=[MetricAggregationType.AVERAGE, MetricAggregationType.TOTAL]
        )
        
        # Track capacity gauge (most recent non-None value)
        latest_capacity_bytes = None
        
        # Track traffic totals (sum of all daily values)
        total_tx = 0
        total_ingress_bytes = 0
        total_egress_bytes = 0
        
        for metrics_query_result in response:
            for metric in metrics_query_result.metrics:
                if metric.name == "UsedCapacity":
                    for timeseries in metric.timeseries:
                        for data in timeseries.data:
                            if data.average is not None:
                                latest_capacity_bytes = data.average
                elif metric.name == "Transactions":
                    for timeseries in metric.timeseries:
                        for data in timeseries.data:
                            if data.total is not None:
                                total_tx += int(data.total)
                elif metric.name == "Ingress":
                    for timeseries in metric.timeseries:
                        for data in timeseries.data:
                            if data.total is not None:
                                total_ingress_bytes += data.total
                elif metric.name == "Egress":
                    for timeseries in metric.timeseries:
                        for data in timeseries.data:
                            if data.total is not None:
                                total_egress_bytes += data.total
                                
        if latest_capacity_bytes is not None:
            capacity_gb = round(latest_capacity_bytes / (1024 ** 3), 3)
        transactions = total_tx
        ingress_gb = round(total_ingress_bytes / (1024 ** 3), 3)
        egress_gb = round(total_egress_bytes / (1024 ** 3), 3)
                            
        return capacity_gb, transactions, ingress_gb, egress_gb
    except Exception as e:
        # Gracefully handle cases where the user lacks Monitoring Reader permissions
        # or when metrics are not available for the resource.
        print(f"  [Warning] Error querying metrics for {resource_id.split('/')[-1]}: {e}", flush=True)
        return "N/A", "N/A", "N/A", "N/A"

def check_system_managed(account_name, resource_group, tags):
    """Detect if the storage account is managed by Azure services (Databricks, AKS, Spring Apps, HDInsight, Synapse)."""
    rg_lower = resource_group.lower()
    
    # 1. Databricks Managed
    if rg_lower.startswith("db-") or "databricks" in rg_lower:
        return True, "Databricks"
    if tags:
        for k, v in tags.items():
            if "databricks" in k.lower() or (v and "databricks" in v.lower()):
                return True, "Databricks"
                
    # 2. Azure Kubernetes Service (AKS) Managed
    if rg_lower.startswith("mc_"):
        return True, "AKS"
    if tags:
        for k, v in tags.items():
            if "aks" in k.lower() or "kubernetes" in k.lower() or (v and ("aks" in v.lower() or "kubernetes" in v.lower())):
                return True, "AKS"
                
    # 3. Azure Spring Apps Managed
    if rg_lower.startswith("ap-svc-rt-") or "spring-cloud" in rg_lower or "springapps" in rg_lower:
        return True, "Azure Spring Apps"
        
    # 4. HDInsight Managed
    if "hdinsight" in rg_lower:
        return True, "HDInsight"
    if tags:
        for k, v in tags.items():
            if "hdinsight" in k.lower() or (v and "hdinsight" in v.lower()):
                return True, "HDInsight"
                
    # 5. Synapse Workspace Managed
    if rg_lower.startswith("synapse-") or "workspace-managed-rg" in rg_lower:
        return True, "Synapse"
        
    return False, None

def recommend_tier(capacity_gb, transactions, ingress_gb, egress_gb):
    """Recommend the GPv2 default access tier (Hot, Cool, or Cold) based on 30d usage."""
    if transactions == "N/A":
        return "Hot (Default)"
        
    try:
        tx = int(transactions)
        cap = float(capacity_gb) if capacity_gb != "N/A" else 0.0
        in_gb = float(ingress_gb) if ingress_gb != "N/A" else 0.0
        out_gb = float(egress_gb) if egress_gb != "N/A" else 0.0
    except ValueError:
        return "Hot (Default)"
        
    # Rule 1: High Transaction Volume (> 10,000 txns in 30 days) -> Hot
    # (Since transaction unit cost in Cool/Cold is high, active workloads must be Hot)
    if tx > 10000:
        return "Hot"
        
    # Rule 2: Completely idle (<= 10 transactions and 0 traffic) -> Cold
    # (Cold tier has very cheap storage but highest transaction cost; perfect for archiving)
    if tx <= 10 and in_gb == 0.0 and out_gb == 0.0:
        return "Cold"
        
    # Rule 3: Low transaction but not completely idle -> Cool
    return "Cool"

def scan_storage_accounts():
    config = load_config()
    credential = get_credential()
    
    inventory = []
    metrics_clients = {} # Cache regional MetricsClient instances
    
    print("\nStarting Azure Storage Inventory Scan...")
    
    for sub in config.get("subscriptions", []):
        sub_id = sub.get("id")
        sub_name = sub.get("name")
        print(f"\nScanning Subscription: {sub_name} ({sub_id})...")
        
        try:
            storage_client = StorageManagementClient(credential, sub_id)
            accounts = list(storage_client.storage_accounts.list())
            print(f"Found {len(accounts)} storage accounts in subscription '{sub_name}'.")
            
            for index, account in enumerate(accounts, 1):
                name = account.name
                # Parse resource group from id
                resource_group = account.id.split("/")[4]
                kind = account.kind # e.g. Storage, StorageV2, BlobStorage
                sku = account.sku.name # e.g. Standard_LRS, Premium_LRS
                location = account.location
                access_tier = getattr(account, "access_tier", "N/A")
                is_hns_enabled = getattr(account, "is_hns_enabled", False)
                public_network = getattr(account, "public_network_access", "N/A")
                tags = getattr(account, "tags", {})
                
                print(f"  [{index}/{len(accounts)}] Processing {name} ({kind})...")
                
                # Check if it is managed by a system service
                is_sys_managed, service_name = check_system_managed(name, resource_group, tags)
                
                # Determine migration action
                kind_lower = kind.lower()
                if is_sys_managed:
                    migration_needed = f"No ({service_name} Managed)"
                    migration_action = f"Managed by {service_name}. Will be managed/migrated automatically by the service. No manual action needed."
                elif kind_lower in ["storage", "blobstorage"]:
                    migration_needed = "Yes"
                    migration_action = "Upgrade to StorageV2 (GPv2) required before 13 Oct 2026."
                else:
                    migration_needed = "No"
                    migration_action = "Already on GPv2 or Premium. No upgrade required."
                
                # Fetch usage metrics
                # Normalize location for Metrics regional endpoint
                normalized_loc = location.lower().replace(" ", "")
                if normalized_loc not in metrics_clients:
                    endpoint = f"https://{normalized_loc}.metrics.monitor.azure.com"
                    metrics_clients[normalized_loc] = MetricsClient(endpoint, credential)
                
                client = metrics_clients[normalized_loc]
                capacity_gb, transactions, ingress_gb, egress_gb = query_metrics(client, account.id)
                
                # Identify recommended tier (Hot, Cool, Cold)
                rec_tier = recommend_tier(capacity_gb, transactions, ingress_gb, egress_gb)
                
                inventory.append({
                    "subscription_name": sub_name,
                    "subscription_id": sub_id,
                    "resource_group": resource_group,
                    "account_name": name,
                    "location": location,
                    "kind": kind,
                    "sku": sku,
                    "access_tier": access_tier if access_tier else "N/A",
                    "hns_enabled": "Yes" if is_hns_enabled else "No",
                    "public_network": public_network if public_network else "N/A",
                    "migration_needed": migration_needed,
                    "migration_action": migration_action,
                    "capacity_gb": capacity_gb,
                    "transactions": transactions,
                    "ingress_gb": ingress_gb,
                    "egress_gb": egress_gb,
                    "recommended_tier": rec_tier,
                    "report_version": VERSION
                })
        except Exception as e:
            print(f"Error scanning subscription '{sub_name}': {e}")
            
    return inventory

def write_outputs(inventory):
    # Sort inventory: Migration Needed first, then by subscription and name
    inventory.sort(key=lambda x: (x["migration_needed"] != "Yes", x["subscription_name"], x["account_name"]))
    
    # Define timestamped output filenames
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = f"storage_inventory_{timestamp}.csv"
    md_file = f"storage_inventory_{timestamp}.md"
    
    # 1. Write CSV File
    fieldnames = [
        "subscription_name", "subscription_id", "resource_group", "account_name", 
        "location", "kind", "sku", "access_tier", "hns_enabled", "public_network", 
        "migration_needed", "migration_action", "capacity_gb", "transactions", 
        "ingress_gb", "egress_gb", "recommended_tier", "report_version"
    ]
    
    # Write using utf-8-sig (with BOM) so that Microsoft Excel can read Chinese characters properly
    try:
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(inventory)
        print(f"\nCSV Inventory report saved to {csv_file}")
    except PermissionError as e:
        print(f"\n[Error] Permission denied writing to {csv_file}: {e}")
        
    # 2. Write Markdown File
    md_content = []
    md_content.append("# Azure Storage GPv1 Migration Inventory & Analysis Report\n\n")
    md_content.append(f"Report Version: {VERSION}\n")
    md_content.append(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    
    # Summary statistics
    total_accounts = len(inventory)
    migration_required_count = sum(1 for x in inventory if x["migration_needed"] == "Yes")
    system_managed_count = sum(1 for x in inventory if x["migration_needed"] not in ["Yes", "No"])
    already_v2_count = total_accounts - migration_required_count - system_managed_count
    
    md_content.append("## Executive Summary\n\n")
    md_content.append(f"- **Total Storage Accounts Scanned:** {total_accounts}\n")
    md_content.append(f"- **Manual Upgrade Required (GPv1/BlobStorage):** {migration_required_count}\n")
    md_content.append(f"- **System-Managed / Excluded Accounts (Databricks, AKS, etc.):** {system_managed_count}\n")
    md_content.append(f"- **Already Upgraded / Compliant (GPv2/Premium):** {already_v2_count}\n\n")
    
    # Section 1: Action Required
    md_content.append("## ⚠️ Storage Accounts Requiring Manual Upgrade\n\n")
    md_content.append("The following accounts must be upgraded to **GPv2 (StorageV2)** before **13 October 2026** to avoid automatic migration or potential service disruptions.\n\n")
    
    manual_migration_list = [x for x in inventory if x["migration_needed"] == "Yes"]
    if manual_migration_list:
        md_content.append("| 核可標記 | Subscription | Resource Group | Storage Account | Kind | SKU | Capacity (GB) | 30d Transactions | Access Tier (Current) | Recommended GPv2 Tier | Recommended Action |\n")
        md_content.append("| :---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for x in manual_migration_list:
            md_content.append(f"| [ ] | {x['subscription_name']} | {x['resource_group']} | `{x['account_name']}` | {x['kind']} | {x['sku']} | {x['capacity_gb']} | {x['transactions']} | {x['access_tier']} | **{x['recommended_tier']}** | Plan to migrate to GPv2. Set tier to {x['recommended_tier']}. |\n")
    else:
        md_content.append("No storage accounts require manual upgrade! All accounts are compliant.\n")
    md_content.append("\n")
    
    # Section 2: System Managed / Excluded
    md_content.append("## ℹ️ System Managed Storage Accounts (Databricks, AKS, etc.)\n\n")
    md_content.append("These accounts are associated with managed services (e.g. Databricks workspaces, AKS node pools) and will be managed and upgraded automatically by Microsoft or the service. No manual action is required.\n\n")
    
    system_migration_list = [x for x in inventory if x["migration_needed"] not in ["Yes", "No"]]
    if system_migration_list:
        md_content.append("| Subscription | Resource Group | Storage Account | Kind | SKU | Capacity (GB) | 30d Transactions | Managed By |\n")
        md_content.append("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for x in system_migration_list:
            md_content.append(f"| {x['subscription_name']} | {x['resource_group']} | `{x['account_name']}` | {x['kind']} | {x['sku']} | {x['capacity_gb']} | {x['transactions']} | {x['migration_needed']} |\n")
    else:
        md_content.append("No system-managed storage accounts found.\n")
    md_content.append("\n")
    
    # Section 3: All Details
    md_content.append("## Detailed Inventory Table\n\n")
    md_content.append("| Subscription | Resource Group | Storage Account | Location | Kind | SKU | Tier | Recommended Tier | HNS | Public Net | Migration? | Size (GB) | 30d Txn | Ingress (GB) | Egress (GB) |\n")
    md_content.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
    for x in inventory:
        hns = "Yes" if x["hns_enabled"] == "Yes" else "No"
        md_content.append(f"| {x['subscription_name']} | {x['resource_group']} | `{x['account_name']}` | {x['location']} | {x['kind']} | {x['sku']} | {x['access_tier']} | {x['recommended_tier']} | {hns} | {x['public_network']} | {x['migration_needed']} | {x['capacity_gb']} | {x['transactions']} | {x['ingress_gb']} | {x['egress_gb']} |\n")

    full_md_text = "".join(md_content)
    try:
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(full_md_text)
        print(f"Markdown Inventory report saved to {md_file}")
    except PermissionError as e:
        print(f"[Error] Permission denied writing to {md_file}: {e}")

    # Copy latest reports to base filenames
    import shutil
    try:
        shutil.copy2(csv_file, CSV_FILE)
        print(f"Copied latest CSV to {CSV_FILE}")
        shutil.copy2(md_file, MD_FILE)
        print(f"Copied latest Markdown to {MD_FILE}")
    except Exception as e:
        print(f"[Warning] Failed to update main files {CSV_FILE} or {MD_FILE}: {e}")

if __name__ == "__main__":
    inventory_data = scan_storage_accounts()
    if inventory_data:
        write_outputs(inventory_data)
        print("\nScan completed successfully!")
    else:
        print("\nScan yielded no storage account data. Check credentials or configuration.")
