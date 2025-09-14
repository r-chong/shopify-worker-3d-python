## fileCreate logic removed as per requirements

def _first_or_none(seq):
    return seq[0] if seq and len(seq) > 0 else None
"""
Production-ready Shopify 3D model poller

Install dependencies:
    pip install -r requirements.txt
"""

import argparse
import sys
import struct
import time
import os
import json
import hashlib
import requests
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
    def _stage(resource_kind):
        q = """
        mutation($input:[StagedUploadInput!]!){
            stagedUploadsCreate(input:$input){
                stagedTargets{ url resourceUrl parameters{ name value } }
                userErrors{ field message }
            }
        }"""
        input_obj = {
            "resource": resource_kind,
            "filename": filename,
            "mimeType": "model/gltf-binary",
            "httpMethod": "POST"
        }
        if resource_kind == "MODEL_3D":
            input_obj['fileSize'] = str(len(glb_bytes))
        vars_ = {"input": [input_obj]}
        data = gql(q, vars_)
        payload = data["stagedUploadsCreate"]
        errs = payload.get("userErrors") or []
        target = _first_or_none(payload.get("stagedTargets") or [])
        return target, errs

    # Try MODEL_3D first
    target, errs = _stage("MODEL_3D")
    if errs:
        print(f"[stagedUploadsCreate/MODEL_3D] userErrors: {errs}")
    if not target or not target.get("url"):
        # Fallback to FILE if MODEL_3D didn’t provide a proper upload URL
        print("[stagedUploadsCreate] Falling back to resource=FILE…")
        target, errs2 = _stage("FILE")
        if errs2:
            print(f"[stagedUploadsCreate/FILE] userErrors: {errs2}")
        if not target or not target.get("url"):
            raise RuntimeError(
                "stagedUploadsCreate did not return a valid upload URL. "
                f"MODEL_3D errors={errs}, FILE errors={errs2}"
            )

    upload_url = target["url"]
    resource_url = target.get("resourceUrl")
    params = {p["name"]: p["value"] for p in target.get("parameters", [])}

    if not resource_url:
        # If we have an upload URL but no resourceUrl, that’s unusual — surface it early.
        raise RuntimeError(
            "stagedUploadsCreate returned an upload URL but missing resourceUrl; "
            "cannot attach later. Response looked like:\n"
            f"url={upload_url}\nparams_keys={list(params.keys())}"
        )

    # Upload file to Google Cloud (Shopify staged bucket)
    print(f"Uploading GLB file '{filename}' ({len(glb_bytes)} bytes) to Shopify staged URL…")
    files = {"file": (filename, glb_bytes, "model/gltf-binary")}
    up = requests.post(upload_url, data=params, files=files, timeout=60)
    try:
        up.raise_for_status()
    except Exception:
        # Print body for easier diagnosis
        print(f"Staged upload failed [{up.status_code}]: {up.text}")
        raise

    # 200/201/204 are all acceptable from GCS; many return 201 with XML
    print(f"Staged upload OK ({up.status_code}).")
    return resource_url

def attach_model_media(product_id, resource_url):
  m = """
  mutation productCreateMedia($productId:ID!, $media:[CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
          media { id status }
          mediaUserErrors { field message code }
      }
  }"""
  print(f"Attaching 3D model media to product {product_id}…")
  res = gql(m, {
          "productId": product_id,
          "media": [{
                  "originalSource": resource_url,
                  "mediaContentType": "MODEL_3D"
          }]
  })
  info = res.get("productCreateMedia", {})
  errs = info.get("mediaUserErrors") or []
  if errs:
          # include a hint if it looks like the classic invalid-url case
          hint = ""
          if any("Invalid Model 3d url" in (e.get("message") or "") for e in errs):
                  hint = (" HINT: Ensure you passed the *Shopify* staged `resourceUrl` "
                                  "from the *same* stagedUploadsCreate call, filename ends with .glb, "
                                  "and mime is model/gltf-binary.")
          raise RuntimeError(f"productCreateMedia errors: {errs}.{hint}")

  media = _first_or_none(info.get("media") or [])
  mid = media.get("id") if media else None
  mstatus = media.get("status") if media else None
  print(f"Media attached: id={mid} status={mstatus}")
  return mid, mstatus

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

  print(f"→ Generating 3D for: {p['title']} (ID: {pid})")
  try:
      set_meta(pid, "status", "single_line_text_field", "processing")
  except Exception:
      pass

  try:
      # Generate GLB from image
      print(f"Requesting Meshy 3D model for image: {img['url']}")
      glb = meshy_generate_glb(img["url"])
      print("Uploading GLB to Shopify staged upload...")
      res_url = staged_upload_glb("auto3d.glb", glb)
      print(f"Staged upload complete. Resource URL: {res_url}")
      # Attach to product using the staged resourceUrl
      mid, mstatus = attach_model_media(pid, res_url)
      state[pid] = {"last_fp": fp, "status": mstatus or "PROCESSING"}
      save_state()
      print(f"✅ Attached MODEL_3D: {p['title']} (media {mid}, status {mstatus})")
      try:
          set_meta(pid, "status", "single_line_text_field", mstatus or "PROCESSING")
      except Exception:
          print(f"Warning: Could not update status metafield for {p['title']}")
      return bool(mid)
  except Exception as e:
      print(f"❌ Error processing {p['title']}: {e}")
      state[pid] = {"last_fp": fp, "status": "failed", "error": str(e)}
      save_state()
      try:
          set_meta(pid, "status", "single_line_text_field", f"error:{str(e)[:120]}")
      except Exception:
          print(f"Warning: Could not update error status metafield for {p['title']}")
      return False

if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--product", help="Shopify product GID to process")
  parser.add_argument("--file", help="Local GLB file to upload")
  args = parser.parse_args()

  print("Local poller running. Ctrl+C to stop.")
  if args.product:
      print(f"Processing single product: {args.product}")
      # Fetch product by GID
      q = """
      query($id:ID!){
        product(id:$id){
          id title updatedAt images(first:5){ edges{ node{ id url } } } media(first:10){ edges{ node{ mediaContentType } } }
        }
      }
      """
      data = gql(q, {"id": args.product})
      p = data.get("product")
      if not p:
          print(f"Product not found: {args.product}")
          sys.exit(1)
      process_product(p)
      sys.exit(0)
  loop_count = 0
  while True:
      loop_count += 1
      print(f"\n--- Polling loop #{loop_count} ---")
      try:
          products = list_recent_products(limit=15)
          print(f"Found {len(products)} products to check.")
          for p in products:
              print(f"Checking product: {p['title']} (ID: {p['id']})")
              process_product(p)
      except Exception as e:
          print("Loop error:", e)
      print("Sleeping for 5 seconds before next poll...")
      time.sleep(5)  # poll every 5 seconds
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
  mutation stagedUploadsCreate($input:[StagedUploadInput!]!){
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
  # Print GLB file size for debugging
  print(f"Uploading GLB file '{filename}' ({len(glb_bytes)} bytes) to Shopify staged URL...")
  files = {"file": (filename, glb_bytes, "model/gltf-binary")}
  up = requests.post(t["url"], data=form, files=files)
  print(f"Staged upload response status: {up.status_code}")
  if up.status_code != 200:
      print(f"Staged upload error: {up.text}")
  up.raise_for_status()
  return t["resourceUrl"]

def attach_model_media(product_id, resource_url):
  # Attach a 3D model to the product using productCreateMedia mutation
  m = """
  mutation productCreateMedia($productId:ID!, $media:[CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
          media {
              id
              status
          }
          userErrors {
              field
              message
          }
      }
  }
  """
  print(f"Attaching 3D model media to product {product_id}...")
  result = gql(m, {
          "productId": product_id,
          "media": [{"originalSource": resource_url, "mediaContentType": "MODEL_3D"}]
  })
  # Print feedback about media attachment
  media_info = result.get("productCreateMedia", {})
  if media_info.get("userErrors"):
          print("Shopify media attachment errors:", media_info["userErrors"])
  else:
          print("Media attached:", media_info.get("media"))

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

  print(f"→ Generating 3D for: {p['title']} (ID: {pid})")
  try:
      set_meta(pid, "status", "single_line_text_field", "processing")
  except Exception:
      pass

  try:
      # Generate GLB from image
      print(f"Requesting Meshy 3D model for image: {img['url']}")
      glb = meshy_generate_glb(img["url"])
      print("Uploading GLB to Shopify staged upload...")
      res_url = staged_upload_glb("auto3d.glb", glb)
      print(f"Staged upload complete. Resource URL: {res_url}")
      attach_model_media(pid, res_url)
      state[pid] = {"last_fp": fp, "status": "ready"}
      save_state()
      try:
          set_meta(pid, "status", "single_line_text_field", "ready")
      except Exception:
          print(f"Warning: Could not update status metafield for {p['title']}")
      print(f"✅ Attached MODEL_3D: {p['title']}")
      return True
  except Exception as e:
      print(f"❌ Error processing {p['title']}: {e}")
      state[pid] = {"last_fp": fp, "status": "failed", "error": str(e)}
      save_state()
      try:
          set_meta(pid, "status", "single_line_text_field", f"error:{str(e)[:120]}")
      except Exception:
          print(f"Warning: Could not update error status metafield for {p['title']}")
      return False

if __name__ == "__main__":
  print("Local poller running. Ctrl+C to stop.")
  loop_count = 0
  while True:
      loop_count += 1
      print(f"\n--- Polling loop #{loop_count} ---")
      try:
          products = list_recent_products(limit=15)
          print(f"Found {len(products)} products to check.")
          for p in products:
              print(f"Checking product: {p['title']} (ID: {p['id']})")
              process_product(p)
      except Exception as e:
          print("Loop error:", e)
      print("Sleeping for 5 seconds before next poll...")
      time.sleep(5)  # poll every 5 seconds
