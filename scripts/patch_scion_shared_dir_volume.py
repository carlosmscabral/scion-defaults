#!/usr/bin/env python3
"""Keep the NFS claim volume of a read-only shared dir read-write in Scion's Kubernetes runtime.

A read-only shared dir (the factory's `knowledge-plane`) sits on the same NFS claim as the agent
workspace. When Scion marks that shared dir's pod *volume* read-only, the node mounts the export
read-only and the workspace mount of the same export shares that state, so the
`workspace-provision` init container fails with "read-only file system" and no agent pod starts
(hillclimb Run 1, fifth attempt). The shared dir's *volumeMount* stays read-only, which is what
keeps agents from writing to it.

Usage: patch_scion_shared_dir_volume.py <scion-src>/pkg/runtime/k8s_runtime.go

Exits 0 when the file is patched or already patched, and 1 when it has shared-dir claim code this
script does not recognise. Upstream has renamed this code before; a silent no-op costs a full
cold start to discover.
"""
import pathlib
import re
import sys

# Scion after the k8s_nfs_volume.go refactor: the volume is built by sharedDirPodVolume(...).
NEW_CALL = "sdVolume, sdSubPath, err := sharedDirPodVolume(config.SharedDirStorage, i, sd.Name, sd.ReadOnly, target)"
NEW_PATCHED = "sdVolume, sdSubPath, err := sharedDirPodVolume(config.SharedDirStorage, i, sd.Name, false, target)"

# Older Scion: the claim volume source is written inline.
OLD_PAT = re.compile(
    r"(if config\.SharedDirStorage\.Shared\(\) \{[\s\S]*?PersistentVolumeClaim:\s*&corev1\.PersistentVolumeClaimVolumeSource\{\n"
    r"[ \t]*ClaimName:\s*claim,\n)"
    r"[ \t]*ReadOnly:\s*sd\.ReadOnly,\n"
)
OLD_PATCHED = re.compile(r"ClaimName:\s*claim,\n[ \t]*\},")


def patch(src: str) -> tuple[str, str]:
    """Return (new_source, status); status is 'patched', 'already' or 'unrecognised'."""
    if NEW_CALL in src:
        return src.replace(NEW_CALL, NEW_PATCHED, 1), "patched"
    updated, count = OLD_PAT.subn(r"\1", src, count=1)
    if count == 1:
        return updated, "patched"
    if NEW_PATCHED in src or OLD_PATCHED.search(src):
        return src, "already"
    if "SharedDirStorage.Shared()" not in src:
        # No shared-dir claim code at all in this file: nothing to keep read-write.
        return src, "already"
    return src, "unrecognised"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1])
    new_src, status = patch(path.read_text())
    if status == "patched":
        path.write_text(new_src)
    if status == "unrecognised":
        print(
            f"ERROR: {path} has shared-dir claim volume code this patch does not recognise. "
            "A read-only shared dir would make the agent workspace read-only and no agent pod would start. "
            "Update scripts/patch_scion_shared_dir_volume.py for this Scion revision, or pin SCION_REF.",
            file=sys.stderr,
        )
        return 1
    print(f"shared-dir claim volume patch: {status} ({path.name})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
