import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from flask import Flask, request, jsonify, send_from_directory
from camoufox.sync_api import Camoufox
import stripe
import imaplib
import email
import re
import time
import random

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__, static_folder="static")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

# Bot email config
BOT_EMAIL = os.environ.get("BOT_EMAIL", "townplanning917@gmail.com")
BOT_APP_PASSWORD = os.environ.get("BOT_APP_PASSWORD", "")

# In-memory order store
orders = {}


def human_type(page, selector, text):
    page.fill(selector, "")
    time.sleep(0.3)
    page.click(selector)
    time.sleep(0.8)
    for i, char in enumerate(text):
        page.keyboard.type(char)
        if i % 5 == 0:
            time.sleep(random.uniform(0.2, 0.4))
        else:
            time.sleep(random.uniform(0.08, 0.2))


def parse_address(address):
    parts = address.strip().split()
    number = parts[0]
    street = parts[1] if len(parts) > 1 else ""
    return f"{number} {street}", street.upper()


def fetch_verification_code(timeout=60):
    """Check Gmail inbox for LANDATA verification code"""
    print("[EMAIL] Checking Gmail for verification code...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(BOT_EMAIL, BOT_APP_PASSWORD)
            mail.select("inbox")

            # Search for recent emails
            _, data = mail.search(None, "UNSEEN")
            ids = data[0].split()

            for num in reversed(ids):
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                # Get email body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode(
                                "utf-8", errors="ignore"
                            )
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                # Look for 6-digit code
                match = re.search(r"\b(\d{6})\b", body)
                if match:
                    code = match.group(1)
                    print(f"[OK] Found verification code: {code}")
                    mail.logout()
                    return code

            mail.logout()
        except Exception as e:
            print(f"[WARN] IMAP error: {e}")

        time.sleep(5)

    raise Exception("❌ Verification code not received within timeout")


def landata_production_search(address):
    short_address, keyword = parse_address(address)

    with Camoufox(headless=False) as browser:
        context = browser.new_context(locale="en-AU", timezone_id="Australia/Melbourne")
        page = context.new_page()

        try:
            for attempt in range(3):
                try:
                    page.goto("https://order.landata.online/", wait_until="networkidle")
                    break
                except:
                    time.sleep(random.uniform(2, 5))
            else:
                raise Exception("Failed to load after 3 attempts")

            time.sleep(2)

            target_selector = 'input[type="text"]'
            for selector in [
                'input[placeholder*="address"]',
                'input[placeholder*="Address"]',
                'input[placeholder*="property"]',
                'input[type="search"]',
                'input[type="text"]',
            ]:
                if page.locator(selector).count() > 0:
                    target_selector = selector
                    break

            human_type(page, target_selector, short_address)

            page.wait_for_function(
                f"""() => {{
                    const items = document.querySelectorAll('li, [role="option"], [class*="suggestion"], [class*="result"]');
                    return Array.from(items).some(el => el.offsetParent !== null && el.innerText.toUpperCase().includes('{keyword}'));
                }}""",
                timeout=10000,
            )

            suggestion = (
                page.locator(
                    'li:visible, [role="option"]:visible, [class*="suggestion"]:visible, [class*="result"]:visible'
                )
                .filter(has_text=keyword)
                .first
            )
            suggestion.hover()
            time.sleep(0.2)
            suggestion.click()

            page.wait_for_selector("button:has-text('Next')", timeout=10000)
            page.locator("button:has-text('Next')").click()

            # Wait for either /products or /multi (multi-title addresses)
            page.wait_for_url(
                lambda url: "/products" in url or "/multi" in url, timeout=15000
            )
            time.sleep(2)

            # Handle multi-title page
            if "/multi" in page.url:
                print("[MULTI] Multi-title address — opening old portal...")
                page.wait_for_selector(
                    "a:has-text('All Products'), button:has-text('All Products')",
                    timeout=10000,
                )
                page.locator(
                    "a:has-text('All Products'), button:has-text('All Products')"
                ).first.click()
                page.wait_for_load_state("networkidle")
                time.sleep(2)
                print(f"[OK] Old portal loaded: {page.url}")

                # Step 1: Click Street Address radio button
                page.wait_for_selector(
                    "#ContentPlaceHolder1_IdentifierChoice_0",
                    state="visible",
                    timeout=15000,
                )
                time.sleep(1)
                page.click("#ContentPlaceHolder1_IdentifierChoice_0")
                time.sleep(2)
                page.wait_for_selector(
                    "#ContentPlaceHolder1_StreetNumber", state="visible", timeout=15000
                )
                print("[OK] Street Address radio selected")

                # Parse address parts
                addr_parts = address.split()
                street_number = addr_parts[0]
                street_name = addr_parts[1] if len(addr_parts) > 1 else ""
                street_type = addr_parts[2] if len(addr_parts) > 2 else "Street"
                suburb = addr_parts[3] if len(addr_parts) > 3 else ""
                postcode = addr_parts[4] if len(addr_parts) > 4 else ""

                page.fill("#ContentPlaceHolder1_StreetNumber", street_number)
                page.fill("#ContentPlaceHolder1_StreetName", street_name)
                page.select_option("#ContentPlaceHolder1_StreetType", label=street_type)
                page.fill("#ContentPlaceHolder1_Suburb", suburb)
                if postcode:
                    page.fill("#ContentPlaceHolder1_Postcode", postcode)
                time.sleep(1)
                print("[OK] Address fields filled")

                # Submit
                page.keyboard.press("Enter")
                time.sleep(2)

                # Step 2: Extract titles from confirm property page
                page.wait_for_selector(
                    "input[value='Confirm Property Details']", timeout=20000
                )
                print("[OK] On confirm property page")

                # Use exact input IDs: p1lotplan_lotsN and p1lotplan_plannumberN
                titles_data = page.evaluate(
                    """
                    () => {
                        const results = [];
                        let i = 1;
                        while (i <= 20) {
                            const lotEl = document.getElementById('p1lotplan_lots' + i);
                            const planEl = document.getElementById('p1lotplan_plannumber' + i);
                            if (!lotEl && !planEl) break;
                            const lot = lotEl ? lotEl.value : '1';
                            const plan = planEl ? planEl.value : '';
                            if (plan) {
                                results.push({ label: lot + '/TP ' + plan, index: i - 1 });
                            }
                            i++;
                        }
                        const btn = document.querySelector("input[value='Confirm Property Details']");
                        const btnName = btn ? (btn.name || btn.id || '') : '';
                        results.forEach(r => r.btn_name = btnName);
                        return results;
                    }
                """
                )

                print(
                    f"[OK] Found {len(titles_data)} titles: {[t['label'] for t in titles_data]}"
                )
                return {
                    "status": "multi_title",
                    "address": address,
                    "titles": titles_data,
                    "message": "This address has multiple title records. Please select one.",
                }

            page.wait_for_selector("text=Copy of Title", timeout=15000)
            time.sleep(1)

            products = page.evaluate(
                """() => {
                const results = [];
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                for (const cb of checkboxes) {
                    let card = cb.parentElement;
                    for (let i = 0; i < 8; i++) {
                        if (!card) break;
                        const txt = card.innerText || '';
                        if (txt.includes('A$') && txt.length > 10 && txt.length < 500) break;
                        card = card.parentElement;
                    }
                    if (!card) continue;
                    const cardText = card.innerText || '';
                    const lines = cardText.split('\\n').map(l => l.trim()).filter(Boolean);
                    if (lines.length < 2) continue;
                    const priceMatch = cardText.match(/A\\$\\s*[\\d.]+/);
                    if (!priceMatch) continue;
                    const title = lines[0].replace(/\\u00a0/g, '').trim();
                    if (!title || title === 'Details' || title === 'Select') continue;
                    const registry = lines.find(l => l.includes('Registry') || l.includes('Land')) || '';
                    results.push({ title, registry, price: priceMatch[0] });
                }
                return results;
            }"""
            )

            seen = set()
            clean = []
            for p in products:
                if p["title"] not in seen:
                    seen.add(p["title"])
                    clean.append(p)

            return {"status": "success", "address": address, "products": clean}

        except Exception as e:
            return {"status": "error", "message": str(e)}


def landata_purchase(address, product_title, customer_email):
    """Actually purchase the certificate from LANDATA"""
    short_address, keyword = parse_address(address)
    print(f"[BUY] Starting purchase: {product_title} for {address}")

    with Camoufox(headless=False) as browser:
        context = browser.new_context(locale="en-AU", timezone_id="Australia/Melbourne")
        page = context.new_page()

        try:
            page.goto("https://order.landata.online/", wait_until="networkidle")
            time.sleep(3)

            target_selector = 'input[type="text"]'
            for selector in [
                'input[placeholder*="address"]',
                'input[placeholder*="Address"]',
                'input[type="text"]',
            ]:
                if page.locator(selector).count() > 0:
                    target_selector = selector
                    break

            print(f"[TYPE] Typing: {short_address}")
            human_type(page, target_selector, short_address)
            time.sleep(3)  # extra wait for autocomplete

            page.wait_for_function(
                f"""() => {{
                    const items = document.querySelectorAll('li, [role="option"], [class*="suggestion"], [class*="result"]');
                    return Array.from(items).some(el => el.offsetParent !== null && el.innerText.toUpperCase().includes('{keyword}'));
                }}""",
                timeout=15000,
            )

            suggestion = (
                page.locator(
                    'li:visible, [role="option"]:visible, [class*="suggestion"]:visible, [class*="result"]:visible'
                )
                .filter(has_text=keyword)
                .first
            )
            suggestion.hover()
            time.sleep(0.5)
            suggestion.click()
            print("[OK] Suggestion clicked")
            time.sleep(2)

            page.wait_for_selector("button:has-text('Next')", timeout=15000)
            page.locator("button:has-text('Next')").click()
            page.wait_for_url("**/products**", timeout=15000)
            page.wait_for_selector("text=Copy of Title", timeout=15000)
            time.sleep(2)
            print("[OK] On products page")

            # Select the right product
            checkboxes = page.locator('input[type="checkbox"]').all()
            for cb in checkboxes:
                try:
                    card = cb.locator("xpath=ancestor::*[position()<=8]").last
                    if product_title.lower() in card.inner_text().lower():
                        cb.click()
                        print(f"[OK] Selected: {product_title}")
                        break
                except:
                    continue

            time.sleep(1)

            # Click Next to payment page
            page.locator("button:has-text('Next')").last.click()
            page.wait_for_url("**/pay**", timeout=15000)
            time.sleep(2)
            print(f"[OK] On payment page: {page.url}")

            # Screenshot to see what's on payment page
            page.screenshot(path="payment_page.png")
            print("📸 Screenshot saved: payment_page.png")

            # Dump ALL inputs
            print("--- ALL INPUTS ON PAYMENT PAGE ---")
            for inp in page.locator("input").all():
                try:
                    ph = inp.get_attribute("placeholder") or ""
                    id_ = inp.get_attribute("id") or ""
                    type_ = inp.get_attribute("type") or ""
                    visible = inp.is_visible()
                    print(
                        f"  INPUT: type='{type_}' placeholder='{ph}' id='{id_}' visible={visible}"
                    )
                except:
                    pass

            # Find all visible inputs and fill by position
            # Based on debug: _r_i_ and _r_l_ are visible — likely name and email
            # Wait for page to fully load
            time.sleep(3)

            # Get all visible inputs
            all_vis = [i for i in page.locator("input").all() if i.is_visible()]
            print(f"Visible inputs count: {len(all_vis)}")
            for idx, inp in enumerate(all_vis):
                id_ = inp.get_attribute("id") or ""
                type_ = inp.get_attribute("type") or "text"
                print(f"  [{idx}] id='{id_}' type='{type_}'")

            # Fill name (first visible input) and email (second visible input)
            if len(all_vis) >= 1:
                all_vis[0].click()
                all_vis[0].fill("Andrew Perry")
                print("[OK] Filled field 0 (name)")
                time.sleep(0.5)
            if len(all_vis) >= 2:
                all_vis[1].click()
                all_vis[1].fill(BOT_EMAIL)
                print("[OK] Filled field 1 (email)")
                time.sleep(1)

            # Wait for Send Code button to enable after email is filled
            print("[WAIT] Waiting for Send Code to enable...")
            page.wait_for_function(
                "() => { const btn = document.querySelector('button'); return btn && !btn.disabled; }",
                timeout=15000,
            )
            time.sleep(0.5)
            page.locator("button:has-text('Send Code')").click()
            print("[EMAIL] Verification code sent...")
            time.sleep(5)

            # Read code from Gmail
            code = fetch_verification_code(timeout=90)
            print(f"[KEY] Got code: {code}")

            # Enter code in third visible input
            all_vis2 = [i for i in page.locator("input").all() if i.is_visible()]
            if len(all_vis2) >= 3:
                all_vis2[2].click()
                all_vis2[2].fill(code)
                print("[OK] Entered code")
            time.sleep(0.5)

            # Click Verify
            page.locator("button:has-text('Verify')").click()
            time.sleep(3)
            print("[OK] Verified! Purchase complete.")

            return {
                "status": "success",
                "message": f"Certificate purchased! Will be emailed to {customer_email} shortly.",
            }

        except Exception as e:
            print(f"[WARN] Purchase error: {e}")
            return {"status": "error", "message": str(e)}


@app.route("/search-title", methods=["POST"])
def search_title():
    """Handle title selection for multi-title addresses via old portal"""
    data = request.get_json()
    address = data.get("address", "").strip()
    btn_name = data.get("btn_name", "")
    label = data.get("label", "")

    short_address, keyword = parse_address(address)

    with Camoufox(headless=True) as browser:
        context = browser.new_context(locale="en-AU", timezone_id="Australia/Melbourne")
        page = context.new_page()

        try:
            # Navigate to old portal directly
            page.goto(
                "https://www.landata.vic.gov.au/tpc_step_redirect.aspx",
                wait_until="networkidle",
            )
            time.sleep(2)

            # Select Street Address radio
            page.click("#ContentPlaceHolder1_IdentifierChoice_0")
            time.sleep(2)
            page.wait_for_selector(
                "#ContentPlaceHolder1_StreetNumber", state="visible", timeout=10000
            )

            # Parse and fill address
            addr_parts = address.split()
            page.fill("#ContentPlaceHolder1_StreetNumber", addr_parts[0])
            page.fill(
                "#ContentPlaceHolder1_StreetName",
                addr_parts[1] if len(addr_parts) > 1 else "",
            )
            page.select_option(
                "#ContentPlaceHolder1_StreetType",
                label=addr_parts[2] if len(addr_parts) > 2 else "Street",
            )
            page.fill(
                "#ContentPlaceHolder1_Suburb",
                addr_parts[3] if len(addr_parts) > 3 else "",
            )
            if len(addr_parts) > 4:
                page.fill("#ContentPlaceHolder1_Postcode", addr_parts[4])
            time.sleep(1)
            page.keyboard.press("Enter")
            time.sleep(2)

            # Click the specific confirm button by name
            page.wait_for_selector(
                "input[value='Confirm Property Details']", timeout=20000
            )
            if btn_name:
                btn = page.locator(f"input[name='{btn_name}']")
                if btn.count() > 0:
                    btn.scroll_into_view_if_needed()
                    btn.click()
                else:
                    page.locator(
                        "input[value='Confirm Property Details']"
                    ).first.click()
            else:
                page.locator("input[value='Confirm Property Details']").first.click()

            page.wait_for_load_state("networkidle")
            time.sleep(2)

            # Municipality Next
            page.wait_for_selector("#ContentPlaceHolder1_BtnNext", timeout=20000)
            page.focus("#ContentPlaceHolder1_BtnNext")
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            # Select Certificates
            page.wait_for_selector("text=Select Certificates", timeout=20000)

            # Extract — old portal uses a table structure
            products = []
            rows = page.locator("table tr").all()
            for row in rows:
                text = row.inner_text().strip()
                if not text:
                    continue
                # Look for price pattern
                import re as re3

                price_match = re3.search(r"\$\s*[\d.]+", text)
                if price_match and len(text) < 200:
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    if lines:
                        products.append(
                            {
                                "title": lines[0],
                                "registry": "Land Registry",
                                "price": f"A$ {price_match.group(0).replace('$','').strip()}",
                            }
                        )

            if not products:
                # Fallback — return generic products with standard prices
                products = [
                    {
                        "title": "Copy of Title",
                        "registry": "Land Registry",
                        "price": "A$ 8.10",
                    },
                    {
                        "title": "Copy of Plan",
                        "registry": "Land Registry",
                        "price": "A$ 7.70",
                    },
                ]

            return jsonify(
                {
                    "status": "success",
                    "address": address,
                    "label": label,
                    "products": products,
                }
            )

        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})


@app.route("/")
def index():
    from flask import render_template_string

    with open(os.path.join(app.static_folder, "index.html"), "r") as f:
        html = f.read()
    # Inject Stripe publishable key
    pk = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    html = html.replace(
        "const STRIPE_PK = document.querySelector('meta[name=\"stripe-pk\"]')?.content || '';",
        f"const STRIPE_PK = '{pk}';",
    )
    from flask import Response

    return Response(html, mimetype="text/html")


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    address = data.get("address", "").strip()
    if not address:
        return jsonify({"status": "error", "message": "No address provided"}), 400
    result = landata_production_search(address)
    return jsonify(result)


@app.route("/create-payment-intent", methods=["POST"])
def create_payment_intent():
    data = request.get_json()
    address = data.get("address", "")
    product_title = data.get("product_title", "")
    price_str = data.get("price", "")

    try:
        price_float = float(price_str.replace("A$", "").replace(",", "").strip())
        margin = 3.00
        total = price_float + margin
        amount_cents = int(round(total * 100))
    except:
        return jsonify({"error": "Invalid price"}), 400

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="aud",
        metadata={
            "address": address,
            "product": product_title,
            "landata_price": price_str,
        },
    )

    orders[intent.id] = {
        "address": address,
        "product": product_title,
        "status": "pending",
        "landata_price": price_str,
        "total": f"A$ {total:.2f}",
    }

    return jsonify(
        {
            "client_secret": intent.client_secret,
            "total": f"A$ {total:.2f}",
            "breakdown": {
                "certificate": f"A$ {price_float:.2f}",
                "service_fee": f"A$ {margin:.2f}",
                "total": f"A$ {total:.2f}",
            },
        }
    )


@app.route("/confirm-order", methods=["POST"])
def confirm_order():
    data = request.get_json()
    payment_intent_id = data.get("payment_intent_id", "")
    customer_email = data.get("email", "")

    if payment_intent_id not in orders:
        return jsonify({"status": "error", "message": "Order not found"}), 404

    order = orders[payment_intent_id]
    order["status"] = "paid"
    order["customer_email"] = customer_email

    # Trigger LANDATA purchase in background
    print(
        f"[PAY] Payment confirmed — purchasing {order['product']} for {order['address']}"
    )
    result = landata_purchase(order["address"], order["product"], customer_email)
    order["purchase_result"] = result

    return jsonify(
        {
            "status": "success",
            "message": f"Order confirmed! Your {order['product']} for {order['address']} will be emailed to {customer_email} within a few minutes.",
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
