import os.path
from art import text2art
import questionary
import requests
import subprocess
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import time
import json
from email.utils import parseaddr

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TaskProgressColumn


SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

RULES_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config", "rules.json")
)




def auth():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token.json", "w") as token:
            token.write(creds.to_json())
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", SCOPES
    )
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as token:
        token.write(creds.to_json())
    return creds



def gmail_service(creds=None):
    if creds is None:
        creds = auth()
    return build("gmail", "v1", credentials=creds)




def sign_in():
    creds = auth()
    service = gmail_service(creds)
    profile = service.users().getProfile(userId="me").execute()
    print("Signed in")
    return profile










def get_message_ids(service=None):
    message_id_list = []
    try:
        if service is None:
            service = gmail_service()

        list_throttle = _GmailThrottle()

        # First call to get initial batch and message count (try to get all, or estimate a max)
        initial_results = list_throttle.run(
            lambda: service.users().messages().list(
                userId="me",
                maxResults=500,
                q="in:inbox"
            ).execute()
        )
        total_messages = initial_results.get("resultSizeEstimate", 100)  # Fallback to 100 if not available

        # Start collecting message ids with a rich progress bar
        with Progress(
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TextColumn("Collecting message IDs..."),
        ) as progress:
            task = progress.add_task("[cyan]Collecting message IDs...", total=total_messages)

            results = initial_results
            messages = results.get("messages", [])
            message_id_list.extend(messages)
            progress.update(task, advance=len(messages))

            while "nextPageToken" in results:
                page_token = results["nextPageToken"]
                try:
                    results = list_throttle.run(
                        lambda token=page_token: service.users().messages().list(
                            userId="me",
                            maxResults=500,
                            pageToken=token,
                            q="in:inbox",
                        ).execute()
                    )
                    new_messages = results.get("messages", [])
                    messages.extend(new_messages)
                    message_id_list.extend(new_messages)
                    progress.update(task, advance=len(new_messages))
                except Exception as e:
                    print(f"Error fetching next page of messages: {e}")
                    break

            # If for any reason fewer messages than total, make sure progress bar is finished
            progress.update(task, completed=progress.tasks[0].total)

    except Exception as e:
        print(f"An error occurred while retrieving messages: {e}")
        return []

    if not message_id_list:
        return []

    return message_id_list











_INITIAL_BATCH_SIZE = 25
_INITIAL_PAUSE_SEC = 0.2
_MIN_BATCH_SIZE = 5
_MAX_BATCH_SIZE = 25
_MAX_PAUSE_SEC = 30.0
_RETRY_PAUSE_SEC = 2.0










def _is_retryable_gmail_error(exception: BaseException) -> bool:
    if isinstance(exception, HttpError) and exception.resp.status in (429, 503):
        return True
    msg = str(exception)
    return (
        "rateLimitExceeded" in msg
        or "Too many concurrent" in msg
        or "Service Unavailable" in msg
        or "backendError" in msg
    )


_MAX_ACTION_RETRIES = 8


class _GmailThrottle:
    """Adaptive pause + retry for sequential Gmail API calls."""

    def __init__(self) -> None:
        self.pause = _INITIAL_PAUSE_SEC
        self.consecutive_ok = 0

    def run(self, api_call):
        last_error = None
        for _ in range(_MAX_ACTION_RETRIES):
            try:
                result = api_call()
                self._after_success()
                return result
            except HttpError as e:
                last_error = e
                if _is_retryable_gmail_error(e):
                    self._after_rate_limit()
                    continue
                raise
            except Exception as e:
                if _is_retryable_gmail_error(e):
                    last_error = e
                    self._after_rate_limit()
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("Gmail API call failed after retries")

    def _after_success(self) -> None:
        time.sleep(self.pause)
        self.consecutive_ok += 1
        if self.consecutive_ok >= 2 and self.pause > _INITIAL_PAUSE_SEC:
            self.pause = max(_INITIAL_PAUSE_SEC, self.pause * 0.75)

    def _after_rate_limit(self) -> None:
        self.consecutive_ok = 0
        self.pause = min(_MAX_PAUSE_SEC, max(self.pause * 2, _RETRY_PAUSE_SEC))
        time.sleep(_RETRY_PAUSE_SEC)








def _fetch_message_batch(service, message_ids, responses, progress, task):
    """Fetch metadata for message_ids. Returns IDs that hit retryable errors."""
    rate_limited: list[str] = []

    def callback(request_id, response, exception):
        if exception:
            if _is_retryable_gmail_error(exception):
                if request_id not in rate_limited:
                    rate_limited.append(request_id)
                return
            print(f"Error for {request_id}: {exception}")
            progress.update(task, advance=1)
        else:
            responses[request_id] = response
            progress.update(task, advance=1)

    batch = service.new_batch_http_request(callback=callback)
    pending_count = 0
    seen_in_batch: set[str] = set()
    for message_id in message_ids:
        if message_id in responses:
            progress.update(task, advance=1)
            continue
        if message_id in seen_in_batch:
            continue
        seen_in_batch.add(message_id)
        batch.add(
            service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ),
            request_id=message_id,
        )
        pending_count += 1

    if pending_count:
        batch.execute()

    return rate_limited










def get_emails_by_id():
    service = gmail_service()
    message_id_lists = get_message_ids(service)
    id_list = list(dict.fromkeys(
        msg["id"] for msg in message_id_lists if msg.get("id")
    ))

    responses = {}
    total = len(id_list)
    if total == 0:
        return {"payload": []}

    print(f"Fetching metadata for {total} emails...", flush=True)

    with Progress(
        SpinnerColumn(),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        TextColumn("Getting emails..."),
        refresh_per_second=4,
    ) as progress:
        task = progress.add_task("[cyan]Fetching emails...", total=total, completed=0)
        progress.refresh()

        pending = list(id_list)
        batch_size = _INITIAL_BATCH_SIZE
        pause = _INITIAL_PAUSE_SEC
        consecutive_ok = 0

        while pending:
            rate_limited: list[str] = []

            for i in range(0, len(pending), batch_size):
                chunk = pending[i : i + batch_size]
                chunk_rate_limited = _fetch_message_batch(
                    service, chunk, responses, progress, task
                )
                rate_limited.extend(chunk_rate_limited)

                if chunk_rate_limited:
                    consecutive_ok = 0
                else:
                    consecutive_ok += 1
                    if consecutive_ok >= 2 and batch_size < _MAX_BATCH_SIZE:
                        batch_size = min(_MAX_BATCH_SIZE, batch_size + 5)
                        pause = max(_INITIAL_PAUSE_SEC, pause * 0.75)

                if i + batch_size < len(pending):
                    time.sleep(pause)

            if not rate_limited:
                break

            pending = list(dict.fromkeys(rate_limited))
            batch_size = max(_MIN_BATCH_SIZE, batch_size // 2)
            pause = min(_MAX_PAUSE_SEC, max(pause * 2, _RETRY_PAUSE_SEC))
            consecutive_ok = 0
            time.sleep(_RETRY_PAUSE_SEC)

    payload = []
    for message_id in id_list:
        message = responses.get(message_id)
        if not message:
            continue
        payload.append({
            "id": message.get("id"),
            "snippet": message.get("snippet"),
            "gmail_labels": message.get("labelIds", []),
            "headers": message.get("payload", {}).get("headers", []),
        })

    return {"payload": payload}











def prep_payload(gmail_data=None):
    if gmail_data is None:
        gmail_data = get_emails_by_id()

    with open(RULES_PATH, encoding="utf-8") as f:
        categories = json.load(f)["categories"]

    emails = gmail_data.get("payload", [])
    classify_ready = []

    for email in emails:
        headers = {h["name"]: h["value"] for h in email.get("headers", [])}
        sender = headers.get("From", "")
        _, addr = parseaddr(sender)
        sender_domain = addr.split("@")[-1] if "@" in addr else ""

        classify_ready.append({
            "id": email.get("id"),
            "sender": sender,
            "sender_domain": sender_domain,
            "subject": headers.get("Subject", ""),
            "snippet": email.get("snippet") or "",
            "gmail_labels": email.get("gmail_labels") or [],
            "has_attachments": False,
            "attachment_count": 0,
            "categories": categories,
        })

    fetched = len(emails)
    built = len(classify_ready)
    if built < fetched:
        print(f"Warning: {fetched - built} emails missing from fetch")

    return classify_ready








def _build_label_cache(service, throttle: _GmailThrottle) -> dict[str, str]:
    labels_response = throttle.run(
        lambda: service.users().labels().list(userId="me").execute()
    )
    return {
        label["name"]: label["id"]
        for label in labels_response.get("labels", [])
        if label.get("name") and label.get("id")
    }







def _get_or_create_label(
    service, label_cache: dict[str, str], name: str, throttle: _GmailThrottle
) -> str | None:
    if not name:
        return None
    if name in label_cache:
        return label_cache[name]

    label_obj = throttle.run(
        lambda: service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
    )
    label_id = label_obj["id"]
    label_cache[name] = label_id
    return label_id










def _add_labels(
    service, message_id: str, label_ids: list[str], throttle: _GmailThrottle
) -> None:
    if not label_ids:
        return
    throttle.run(
        lambda: service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": label_ids},
        ).execute()
    )


def _apply_category_label(
    service,
    label_cache: dict[str, str],
    message_id: str,
    category: str,
    throttle: _GmailThrottle,
) -> None:
    label_id = _get_or_create_label(service, label_cache, category, throttle)
    if label_id:
        _add_labels(service, message_id, [label_id], throttle)









def _get_rule_mode():
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            rules_data = json.load(f)
        return rules_data.get("rule", "").lower()
    except Exception:
        return ""


def _get_categories() -> list[dict]:
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            return json.load(f)["categories"]
    except Exception:
        return []


def _normalize_category_rule(cat: dict) -> tuple[str, bool]:
    action = (cat.get("action") or "KEEP").upper()
    important = bool(cat.get("important", False))
    if action == "IMPORTANT":
        return "KEEP", True
    return action, important


def _rule_for_category(category: str) -> tuple[str, bool]:
    categories = _get_categories()
    if not categories:
        return "UNSURE", False

    by_name = {cat["name"]: cat for cat in categories if cat.get("name")}
    if category in by_name:
        return _normalize_category_rule(by_name[category])

    for name, cat in by_name.items():
        if name.lower() == category.lower():
            return _normalize_category_rule(cat)

    unknown = by_name.get("Unknown")
    if unknown:
        return _normalize_category_rule(unknown)
    return "UNSURE", False


def _action_for_category(category: str) -> str:
    action, _ = _rule_for_category(category)
    return action


def _mark_important_if_needed(
    service,
    message_id: str,
    important: bool,
    throttle: _GmailThrottle,
) -> None:
    if important:
        _add_labels(service, message_id, ["STARRED", "IMPORTANT"], throttle)


def _archive_message(
    service,
    message_id: str,
    throttle: _GmailThrottle,
) -> None:
    throttle.run(
        lambda: service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()
    )


def _apply_email_action(
    service,
    label_cache: dict[str, str],
    result: dict,
    throttle: _GmailThrottle,
) -> None:
    message_id = result.get("id")
    if not message_id:
        return

    category = (result.get("category") or "").strip()
    action, important = _rule_for_category(category)

    rule_mode = _get_rule_mode()
    is_dry_run = rule_mode == "dry run"

    # When in dry run mode, only apply labels, never archive, trash, or remove INBOX
    if is_dry_run:
        # For any action, only apply labels as markers
        # Mark review/trash with review_delete, archive with scaned, important/starred, unsure, etc.
        if action == "TRASH":
            label_id = _get_or_create_label(service, label_cache, "review_delete", throttle)
            if label_id:
                _add_labels(service, message_id, [label_id], throttle)
            return
        if action == "ARCHIVE":
            label_id = _get_or_create_label(service, label_cache, "scaned", throttle)
            if label_id:
                _add_labels(service, message_id, [label_id], throttle)
            return
        if action == "KEEP":
            _mark_important_if_needed(service, message_id, important, throttle)
            if category:
                _apply_category_label(service, label_cache, message_id, category, throttle)
            return
        if action == "REVIEW_DELETE":
            label_id = _get_or_create_label(service, label_cache, "review_delete", throttle)
            if label_id:
                _add_labels(service, message_id, [label_id], throttle)
            return
        if action == "UNSURE":
            label_id = _get_or_create_label(service, label_cache, "Unsure", throttle)
            if label_id:
                _add_labels(service, message_id, [label_id], throttle)
            return
        # Unknown action
        print(f"Unknown action '{action}' for email id={message_id}; applying UNSURE label only")
        label_id = _get_or_create_label(service, label_cache, "Unsure", throttle)
        if label_id:
            _add_labels(service, message_id, [label_id], throttle)
        return

    # Not in dry run mode: perform actions from rules.json (via category)
    if action == "TRASH":
        throttle.run(lambda: service.users().messages().trash(userId="me", id=message_id).execute())
        return

    if action == "ARCHIVE":
        label_id = _get_or_create_label(service, label_cache, "scaned", throttle)
        if label_id:
            _add_labels(service, message_id, [label_id], throttle)
        _archive_message(service, message_id, throttle)
        return

    if action == "KEEP":
        _mark_important_if_needed(service, message_id, important, throttle)
        if category:
            _apply_category_label(service, label_cache, message_id, category, throttle)
        _archive_message(service, message_id, throttle)
        return

    if action == "REVIEW_DELETE":
        label_id = _get_or_create_label(service, label_cache, "review_delete", throttle)
        if label_id:
            _add_labels(service, message_id, [label_id], throttle)
        _archive_message(service, message_id, throttle)
        return

    if action == "UNSURE":
        label_id = _get_or_create_label(service, label_cache, "Unsure", throttle)
        if label_id:
            _add_labels(service, message_id, [label_id], throttle)
        _archive_message(service, message_id, throttle)
        return

    print(f"Unknown action '{action}' for email id={message_id}; applying UNSURE")
    label_id = _get_or_create_label(service, label_cache, "Unsure", throttle)
    if label_id:
        _add_labels(service, message_id, [label_id], throttle)
    _archive_message(service, message_id, throttle)









def start_clean():
    creds = auth()
    if not creds or not creds.valid:
        print("Sign in to start cleaning your email")
        return None, None, None

    service = gmail_service(creds)

    times_to_loop = 0
    # Wait for model server to become ready, with timeout/retry logic
    while model_check() != "server is running":
        print("Model server is not ready. Waiting 20 seconds")
        time.sleep(20)
        times_to_loop += 1
        if times_to_loop == 9:
            print("Server didn't run after 3 minutes. Check the server.")
            return None, None, None

    print("Starting to clean")
 

    payloads = prep_payload()
    responses = []
    action_throttle = _GmailThrottle()
    label_cache = _build_label_cache(service, action_throttle)

    total_payloads = len(payloads)
    if total_payloads == 0:
        print("No emails to classify.")
        return service, creds, responses

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TextColumn("{task.fields[email]}"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task(
            "Classifying emails...",
            total=total_payloads,
            email=""
        )
        for idx, payload in enumerate(payloads, 1):
            subject = payload.get("subject", "")[:30] or "(No subject)"
            progress.update(
                task_id,
                email=f"{subject} ({idx}/{total_payloads})",
                completed=idx-1
            )
            email_id = payload.get("id")
            payload_no_id = dict(payload)
            payload_no_id.pop("id", None)
            try:
                response = requests.post(
                    "http://127.0.0.1:8008/classify",
                    json=payload_no_id,
                    timeout=30
                )
                response.raise_for_status()
                response_data = response.json()
                if isinstance(response_data, dict):
                    response_data["id"] = email_id
                    action, important = _rule_for_category(
                        response_data.get("category", "")
                    )
                    response_data["action"] = action
                    response_data["important"] = important
                print("Classification API response:", response_data)
                responses.append(response_data)
                try:
                    _apply_email_action(service, label_cache, response_data, action_throttle)
                except Exception as process_error:
                    print(f"Error applying action for email id={email_id}: {process_error}")
            except requests.RequestException as e:
                print(f"Error contacting classify API: {e}")
                responses.append({"id": email_id, "error": str(e)})
            progress.update(task_id, advance=1)

    return service, creds, responses








def start_model():
    try:
        return subprocess.Popen(
            ["py", "-3.11", "lora_model_server.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        print("error starting model server")
        print(f"server error {e}")
        return None









def model_check():
    try:
        r = requests.get("http://127.0.0.1:8008/health", timeout=5)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "loading":
            return "server is loading"
        if status == "ok":
            return "server is running"
        return "server error"
    except requests.ConnectionError as e:
        print(f"connection error test server{e}")
        return "server not reachable"
    except requests.RequestException as e:
        print(f"server error{e}")
        return "server error"
    




def kill_model(model_server):
    if model_server is not None and model_server.poll() is None:
        model_server.kill()
    



    
    
def main_menu():
    
    model_server = start_model()



    head = text2art("Email Cleaner V1")
        
    print(head)
    print(" Hello, welcome to CDI Email Cleaner.")
    print(" This project was built by TreyLog and runs on a custom fine-tuned model based on Qwen3:4B.")
    print("")
    print(" Disclaimer:")
    print(" This tool can modify your Gmail inbox by labeling, archiving, or trashing emails based on your selected rules.")
    print(" Use it at your own risk. I am not responsible for lost, deleted, or misclassified emails.")
    print("")
    print(" Safety Note:")
    print(" I have designed this project to be as safe as possible, but you should review your rules before running a clean.")
    print(" For best results, start with label-only or review-mode actions before allowing automatic trashing.")
    print("")
    print(" Current Support:")
    print(" - Gmail only")
    print("")
    print(" This is an open-source project. Contributions, bug reports, and suggestions are welcome.")
    print("")
    print(" Have fun cleaning.")
    print("")
    print("------------------------------------------------------------------------------------------------------------")
    print("")






    



    while True:
        print("")
        command = questionary.select("Whats Your Action", 
        choices=[
            "Sign in",
            "Start Clean",
            "Check Model",
            "Change Rules",
            "Switch Label/Action Mode",
            "Quit"
        ]).ask()

        if command == "Check Model":
            print(model_check())

        if command == "Quit":
            kill_model(model_server)
            break

        if command == "Sign in":
            user = sign_in()
            if user is not None:
                print(user['emailAddress'])

        if command == "Start Clean":            
            start_clean()

        if command == "Change Rules":
            try:
                with open(RULES_PATH, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
                categories = rules_data["categories"]

                # Let the user select a category
                category_names = [cat["name"] for cat in categories]
                cat_name = questionary.select(
                    "Select a category to change action:", choices=category_names
                ).ask()
                if not cat_name:
                    print("No category selected.")
                    continue

                # For the selected category, show action options
                actions = set([cat["action"] for cat in categories])
                # Optional: for clarity, can add all potential actions
                all_possible_actions = [
                    "TRASH",
                    "KEEP",
                    "ARCHIVE",
                    "REVIEW_DELETE",
                    "UNSURE",
                ]
                action_choices = sorted(set(list(actions) + all_possible_actions))
                new_action = questionary.select(
                    f"Select new action for '{cat_name}':", choices=action_choices
                ).ask()
                if not new_action:
                    print("No action selected.")
                    continue

                mark_important = False
                if new_action == "KEEP":
                    current_cat = next(
                        (cat for cat in categories if cat["name"] == cat_name), None
                    )
                    default_important = bool(current_cat and current_cat.get("important"))
                    if current_cat and (current_cat.get("action") or "").upper() == "IMPORTANT":
                        default_important = True
                    mark_important = questionary.confirm(
                        "Also star as important?",
                        default=default_important,
                    ).ask()

                updated = False
                for cat in categories:
                    if cat["name"] == cat_name:
                        cat["action"] = new_action
                        if new_action == "KEEP" and mark_important:
                            cat["important"] = True
                        else:
                            cat.pop("important", None)
                        updated = True
                        break

                if updated:
                    with open(RULES_PATH, "w", encoding="utf-8") as f:
                        json.dump(rules_data, f, indent=2)
                    if new_action == "KEEP" and mark_important:
                        print(
                            f"Category '{cat_name}' set to KEEP (star as important)."
                        )
                    else:
                        print(f"Category '{cat_name}' action changed to '{new_action}'.")
                else:
                    print("Category not found or could not update.")

            except Exception as e:
                print(f"Error changing rules: {e}")

        if command == "Switch Label/Action Mode":
            try:
                with open(RULES_PATH, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
                
                current_rule = rules_data.get('rule', '').lower()
                rule_choices = [
                    ("Label Only (Dry Run Mode: safe, only apply labels)", "dry run"),
                    ("Full Actions (Run Mode: applies/archive/trash for real emails)", "run"),
                ]
                # Set default display string based on current state
                current_mode_display = "Label Only (Dry Run)" if current_rule == "dry run" else "Full Actions (Run)"
                print(f"Current mode: {current_mode_display}")

                selected = questionary.select(
                    "Choose mode for email cleaning:",
                    choices=[rc[0] for rc in rule_choices]
                ).ask()
                if not selected:
                    print("No mode selected.")
                    return

                # Map the label to the internal rule value
                for display, rule_value in rule_choices:
                    if selected == display:
                        rules_data["rule"] = rule_value
                        break

                with open(RULES_PATH, "w", encoding="utf-8") as f:
                    json.dump(rules_data, f, indent=2)

                print(f"Rules mode switched to: {selected}")

            except Exception as e:
                print(f"Error switching mode: {e}")











if __name__ == "__main__":
    main_menu()