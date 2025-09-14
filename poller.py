import os, time, json, hashlib, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
SHOP = os.environ["SHOP"]
TOKEN = os.environ["SHOPIFY_ADMIN_TOKEN"]
MESHY = os.environ["MESHY_API_KEY"]
GQL = f"https://{SHOP}/admin/api/2025-04/graphql.json"
HEAD = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

STATE_PATH = Path("auto3d_state.json")
state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

def save_state():
    STATE_PATH.write_text(json.dumps(state, indent=2))

def gql(query, variables=None):
    r = requests.post(GQL, headers=HEAD, json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    j = r.json()
    if "errors" in j: raise RuntimeError(j["errors"])
    return j["data"]

def list_recent_products(limit=15):
        q = """
        query($n:Int!){
            products(first:$n, sortKey:UPDATED_AT, reverse:true){
                edges{ node{
                    id title updatedAt
                    images(first:5){ edges{ node{ id url } } }
                    media(first:10){ edges{ node{
                        mediaContentType
                    } } }
                } }
            }
        }"""
        d = gql(q, {"n": limit})
        return [e["node"] for e in d["products"]["edges"]]

def has_model3d(product):
    for e in product.get("media", {}).get("edges", []):
        if e["node"]["mediaContentType"] == "MODEL_3D":
            return True
    return False

def latest_image(product):
    imgs = [e["node"] for e in product.get("images", {}).get("edges", [])]
    if not imgs:
        return None
    # Just use the first image since products are sorted by updatedAt
    return imgs[0]

def image_fingerprint(img):
    # stable key: image id + url
    base = f'{img["id"]}|{img["url"]}'
    return hashlib.sha256(base.encode()).hexdigest()[:16]

def set_meta(product_id, key, type_, value):
    m = """
    mutation($id:ID!,$ns:String!,$key:String!,$type:String!,$value:String!){
      metafieldsSet(metafields:[{ownerId:$id, namespace:$ns, key:$key, type:$type, value:$value}]){
        userErrors{ field message }
      }
    }"""
    gql(m, {"id": product_id, "ns": "auto3d", "key": key, "type": type_, "value": value})

def staged_upload_glb(filename, glb_bytes):
    q = """
    mutation($input:[StagedUploadInput!]!){
      stagedUploadsCreate(input:$input){
        stagedTargets{ url resourceUrl parameters{ name value } }
        userErrors{ field message }
      }
    }"""
    d = gql(q, {"input": [{
        "resource": "FILE",
        "filename": filename,
        "mimeType": "model/gltf-binary",
        "httpMethod": "POST"
    }]})
    t = d["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in t["parameters"]}
    files = {"file": (filename, glb_bytes, "model/gltf-binary")}
    up = requests.post(t["url"], data=form, files=files)
    up.raise_for_status()
    return t["resourceUrl"]

def attach_model_media(product_id, resource_url):
    m = """
    mutation($productId:ID!,$media:[CreateMediaInput!]!){
      productUpdate(input:{ id:$productId, media:$media }){
        userErrors{ field message }
      }
    }"""
    gql(m, {
        "productId": product_id,
        "media": [{"originalSource": resource_url, "mediaContentType": "MODEL_3D"}]
    })

def meshy_generate_glb(image_url):
    # 1) create task
    r = requests.post(
        "https://api.meshy.ai/openapi/v1/image-to-3d",
        headers={"Authorization": f"Bearer {MESHY}", "Content-Type": "application/json"},
        json={"image_url": image_url, "enable_texture": True}
    )
    r.raise_for_status()
    resp = r.json()
    print('meshy resp',resp)
    if "task_id" in resp:
        task_id = resp["task_id"]
    elif "result" in resp:
        task_id = resp["result"]
    else:
        print("Meshy API error response:", resp)
        raise RuntimeError(f"Meshy API did not return a valid task id. Response: {resp}")
    # 2) poll
    while True:
        t = requests.get(
            f"https://api.meshy.ai/openapi/v1/image-to-3d/{task_id}",
            headers={"Authorization": f"Bearer {MESHY}"}
        ).json()
        print("Meshy polling response:", t)
        status = t.get("status")
        progress = t.get("progress")
        print(f"Meshy task status: {status} | Progress: {progress}")
        if status == "SUCCEEDED":
            url = t.get("model_url") or next((a["url"] for a in t.get("assets", []) if a.get("format") == "glb"), None)
            if not url: raise RuntimeError("No GLB URL from generator")
            return requests.get(url).content
        if status == "FAILED":
            raise RuntimeError(t.get("error", "Meshy failed"))
        time.sleep(4)

def process_product(p):
    pid = p["id"]
    img = latest_image(p)
    if not img: return False
    fp = image_fingerprint(img)
    already = state.get(pid, {}).get("last_fp")
    if already == fp:
        return False
    if has_model3d(p):
        # product already has a 3D model; record state so we don't touch it again
        state[pid] = {"last_fp": fp, "status": "skipped_model_exists"}
        save_state()
        return False

    print(f"→ Generating 3D for: {p['title']}")
    try:
        set_meta(pid, "status", "single_line_text_field", "processing")
    except Exception:
        pass

    try:
        glb = meshy_generate_glb(img["url"])
        res_url = staged_upload_glb("auto3d.glb", glb)
        attach_model_media(pid, res_url)
        state[pid] = {"last_fp": fp, "status": "ready"}
        save_state()
        try:
            set_meta(pid, "status", "single_line_text_field", "ready")
        except Exception:
            pass
        print(f"✅ Attached MODEL_3D: {p['title']}")
        return True
    except Exception as e:
        print(f"❌ Error processing {p['title']}: {e}")
        state[pid] = {"last_fp": fp, "status": "failed", "error": str(e)}
        save_state()
        try:
            set_meta(pid, "status", "single_line_text_field", f"error:{str(e)[:120]}")
        except Exception:
            pass
        return False

if __name__ == "__main__":
    print("Local poller running. Ctrl+C to stop.")
    while True:
        try:
            products = list_recent_products(limit=15)
            for p in products:
                process_product(p)
        except Exception as e:
            print("Loop error:", e)
        time.sleep(5)  # poll every 5 seconds
