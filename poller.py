import os, time, requests, json
from dotenv import load_dotenv

load_dotenv()
SHOP = os.environ["SHOP"]
TOKEN = os.environ["SHOPIFY_ADMIN_TOKEN"]
MESHY = os.environ["MESHY_API_KEY"]

GQL_URL = f"https://{SHOP}/admin/api/2025-04/graphql.json"
HEADERS = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

def gql(query, variables=None):
    r = requests.post(GQL_URL, headers=HEADERS, json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    j = r.json()
    if "errors" in j: raise RuntimeError(j["errors"])
    return j["data"]

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
        "resource": "FILE", "filename": filename,
        "mimeType": "model/gltf-binary", "httpMethod": "POST"
    }]})
    t = d["stagedUploadsCreate"]["stagedTargets"][0]
    files = {}
    data = {p["name"]: p["value"] for p in t["parameters"]}
    files["file"] = (filename, glb_bytes, "model/gltf-binary")
    up = requests.post(t["url"], data=data, files=files)
    up.raise_for_status()
    return t["resourceUrl"]

def attach_model_media(product_id, resource_url):
    m = """
    mutation($productId:ID!,$media:[CreateMediaInput!]!){
      productUpdate(input:{ id:$productId, media:$media }){
        userErrors{ field message }
      }
    }"""
    gql(m, {"productId": product_id,
             "media": [{"originalSource": resource_url, "mediaContentType": "MODEL_3D"}]})

def generate_glb_from_image(image_url):
    # 1) create task
    r = requests.post("https://api.meshy.ai/openapi/v1/image-to-3d",
                      headers={"Authorization": f"Bearer {MESHY}", "Content-Type": "application/json"},
                      json={"image_url": image_url, "enable_texture": True})
    r.raise_for_status()
    task_id = r.json()["task_id"]
    # 2) poll
    while True:
        t = requests.get(f"https://api.meshy.ai/openapi/v1/tasks/{task_id}",
                         headers={"Authorization": f"Bearer {MESHY}"}).json()
        if t.get("status") == "SUCCEEDED":
            url = t.get("model_url") or next((a["url"] for a in t.get("assets", []) if a.get("format")=="glb"), None)
            if not url: raise RuntimeError("No GLB URL from generator")
            glb = requests.get(url).content
            return glb
        if t.get("status") == "FAILED":
            raise RuntimeError(t.get("error","Meshy failed"))
        time.sleep(4)

def work_once():
    q = """
    {
      products(first:20, sortKey:UPDATED_AT, reverse:true){
        edges{ node{
          id title
          metafields(first:10, namespace:"auto3d"){ edges{ node{ key value } } }
        }}
      }
    }"""
    data = gql(q)
    for edge in data["products"]["edges"]:
        p = edge["node"]
        meta = {e["node"]["key"]: e["node"]["value"] for e in (p.get("metafields", {}).get("edges") or [])}
        if meta.get("pending") != "1":
            continue
        image_url = meta.get("image_url")
        if not image_url:
            continue
        try:
            set_meta(p["id"], "status", "single_line_text_field", "processing")
            glb = generate_glb_from_image(image_url)
            res_url = staged_upload_glb("auto3d.glb", glb)
            attach_model_media(p["id"], res_url)
            set_meta(p["id"], "pending", "single_line_text_field", "0")
            set_meta(p["id"], "status",  "single_line_text_field", "ready")
            print("✅ Done:", p["title"])
        except Exception as e:
            print("❌ Error:", p["title"], e)
            set_meta(p["id"], "status", "single_line_text_field", f"error:{str(e)[:120]}")

if __name__ == "__main__":
    print("Polling… Ctrl+C to stop.")
    while True:
        try:
            work_once()
            print("ran")
        except Exception as e:
            print("Loop error:", e)
        time.sleep(5)

