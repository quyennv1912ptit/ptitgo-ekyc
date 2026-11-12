from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="hr16/Raw-Vietnamese-ID",
    repo_type="dataset",
    local_dir="./Raw-Vietnamese-ID"
)