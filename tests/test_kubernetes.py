"""
tests/test_kubernetes.py
------------------------
Tests for the Kubernetes conventions: KubernetesNormalizer, the packaged split
rules, and ``oas2tosca --kubernetes`` end to end on a trimmed Kubernetes-shaped
OpenAPI document.
"""

import copy
import json

import pytest
import yaml

from any2tosca.cli import main_oas
from any2tosca.kubernetes import GVK, RULES_PATH, KubernetesNormalizer
from any2tosca.split import load_rules, skipped_definitions, split_schema

API = "io.k8s.api"
META = "io.k8s.apimachinery.pkg.apis.meta.v1"


def _ref(name):
    return {"$ref": f"#/definitions/{name}"}


def _gvk(group, kind, version="v1"):
    return [{"group": group, "kind": kind, "version": version}]


def _resource(group, kind, version="v1", spec=None, status=None):
    props = {
        "apiVersion": {"type": "string"},
        "kind": {"type": "string"},
        "metadata": _ref(f"{META}.ObjectMeta"),
    }
    if spec:
        props["spec"] = _ref(spec)
    if status:
        props["status"] = _ref(status)
    return {"type": "object", "properties": props, GVK: _gvk(group, kind, version)}


def _obj(**props):
    return {"type": "object", "properties": props}


DEFS = {
    f"{META}.ObjectMeta": _obj(name={"type": "string"}),
    f"{META}.DeleteOptions": {**_obj(dryRun={"type": "string"}),
                              GVK: _gvk("", "DeleteOptions") + _gvk("apps", "DeleteOptions")},
    "io.k8s.apimachinery.pkg.api.resource.Quantity": {"type": "string"},
    "io.k8s.apimachinery.pkg.version.Info": _obj(major={"type": "string"}),
    f"{API}.core.v1.PersistentVolumeClaim": _resource("", "PersistentVolumeClaim",
                                                       spec=f"{API}.core.v1.PersistentVolumeClaimSpec"),
    f"{API}.core.v1.PersistentVolumeClaimSpec": _obj(
        storage=_ref("io.k8s.apimachinery.pkg.api.resource.Quantity"),
        accessModes={"type": "array", "items": {"type": "string"}, "x-kubernetes-list-type": "atomic"},
    ),
    f"{API}.core.v1.PersistentVolumeClaimList": _obj(
        items={"type": "array", "items": _ref(f"{API}.core.v1.PersistentVolumeClaim")}),
    f"{API}.core.v1.Binding": _resource("", "Binding"),
    f"{API}.apps.v1.StatefulSet": _resource("apps", "StatefulSet", spec=f"{API}.apps.v1.StatefulSetSpec",
                                            status=f"{API}.apps.v1.StatefulSetStatus"),
    f"{API}.apps.v1.StatefulSetSpec": _obj(volumeClaimTemplates={
        "type": "array", "items": _ref(f"{API}.core.v1.PersistentVolumeClaim"),
        "x-kubernetes-list-type": "atomic"}),
    f"{API}.apps.v1.StatefulSetStatus": _obj(replicas={"type": "integer"}),
    f"{API}.apps.v1beta1.StatefulSet": _resource("apps", "StatefulSet", "v1beta1"),
    f"{API}.autoscaling.v1.Scale": _resource("autoscaling", "Scale"),
    f"{API}.autoscaling.v1.HorizontalPodAutoscaler": _resource("autoscaling", "HorizontalPodAutoscaler"),
    f"{API}.autoscaling.v2.HorizontalPodAutoscaler": _resource("autoscaling", "HorizontalPodAutoscaler", "v2"),
    f"{API}.resource.v1.ResourceClaim": _resource("resource.k8s.io", "ResourceClaim"),
}


def _collection(path, name):
    return {path: {"get": {}, "post": {"parameters": [
        {"in": "body", "name": "body", "schema": _ref(name)}]}}}


SPEC = {
    "swagger": "2.0",
    "info": {"title": "Kubernetes", "version": "unversioned"},
    "definitions": DEFS,
    "paths": {
        **_collection("/api/v1/namespaces/{namespace}/persistentvolumeclaims",
                      f"{API}.core.v1.PersistentVolumeClaim"),
        **_collection("/apis/apps/v1/namespaces/{namespace}/statefulsets", f"{API}.apps.v1.StatefulSet"),
        **_collection("/apis/apps/v1beta1/namespaces/{namespace}/statefulsets",
                      f"{API}.apps.v1beta1.StatefulSet"),
        **_collection("/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers",
                      f"{API}.autoscaling.v2.HorizontalPodAutoscaler"),
        **_collection("/apis/autoscaling/v1/namespaces/{namespace}/horizontalpodautoscalers",
                      f"{API}.autoscaling.v1.HorizontalPodAutoscaler"),
        **_collection("/apis/resource.k8s.io/v1/namespaces/{namespace}/resourceclaims",
                      f"{API}.resource.v1.ResourceClaim"),
        "/api/v1/namespaces/{namespace}/pods/{name}/binding": {"post": {"parameters": [
            {"in": "body", "name": "body", "schema": _ref(f"{API}.core.v1.Binding")}]}},
    },
}


# ── Normalizer ────────────────────────────────────────────────────────────────

def _normalized():
    nodes = {f"{API}.apps.v1.StatefulSet", f"{API}.core.v1.PersistentVolumeClaim"}
    root, names = KubernetesNormalizer().normalize({"definitions": DEFS}, nodes)
    return root["definitions"], names


def test_normalizer_collapses_single_gvk_to_a_map():
    defs, _ = _normalized()
    assert defs[f"{API}.apps.v1.StatefulSet"][GVK] == {"group": "apps", "kind": "StatefulSet", "version": "v1"}
    assert defs[f"{API}.core.v1.PersistentVolumeClaim"][GVK]["group"] == ""


def test_normalizer_removes_api_version_and_kind_from_node_types():
    defs, _ = _normalized()
    assert set(defs[f"{API}.apps.v1.StatefulSet"]["properties"]) == {"metadata", "spec", "status"}


def test_normalizer_marks_status_read_only():
    defs, _ = _normalized()
    assert defs[f"{API}.apps.v1.StatefulSet"]["properties"]["status"]["readOnly"] is True


def test_normalizer_keeps_data_type_properties():
    defs, _ = _normalized()
    binding = defs[f"{API}.core.v1.Binding"]
    assert binding["properties"] == DEFS[f"{API}.core.v1.Binding"]["properties"]


def test_normalizer_collapses_single_gvk_on_data_types_too():
    defs, _ = _normalized()
    assert defs[f"{API}.core.v1.Binding"][GVK] == {"group": "", "kind": "Binding", "version": "v1"}


def test_normalizer_keeps_multi_entry_gvk_as_a_list():
    defs, _ = _normalized()
    assert defs[f"{META}.DeleteOptions"][GVK] == DEFS[f"{META}.DeleteOptions"][GVK]


def test_normalizer_drops_type_determined_names_from_required():
    schema = {"definitions": {"R": {"type": "object", "required": ["apiVersion", "spec"],
                                    "properties": {"apiVersion": {"type": "string"},
                                                   "spec": {"type": "string"}}}}}
    root, _ = KubernetesNormalizer().normalize(schema, {"R"})
    assert root["definitions"]["R"]["required"] == ["spec"]


def test_normalizer_does_not_modify_its_input():
    before = copy.deepcopy(DEFS)
    _normalized()
    assert DEFS == before


# ── Packaged split rules ──────────────────────────────────────────────────────

def _placement():
    rules = load_rules(RULES_PATH)
    result = split_schema({"definitions": DEFS}, rules, allow_skipped_refs=True)
    return {u.name: set(u.document["definitions"]) for u in result.units}, \
        skipped_definitions({"definitions": DEFS}, rules)


def test_rules_file_is_packaged():
    assert RULES_PATH.is_file()


def test_rules_place_groups_and_packages():
    units, _ = _placement()
    assert units["meta"] == {"ObjectMeta", "DeleteOptions"}
    assert units["resource"] == {"Quantity"}
    assert units["core"] == {"PersistentVolumeClaim", "PersistentVolumeClaimSpec", "Binding"}
    assert units["apps"] == {"StatefulSet", "StatefulSetSpec", "StatefulSetStatus"}


def test_rules_name_the_resource_group_dra():
    units, _ = _placement()
    assert units["dra"] == {"ResourceClaim"}


def test_rules_keep_autoscaling_v2_hpa_and_v1_scale():
    units, skipped = _placement()
    assert units["autoscaling"] == {"Scale", "HorizontalPodAutoscaler"}
    assert f"{API}.autoscaling.v1.HorizontalPodAutoscaler" in skipped


def test_rules_skip_lists_betas_and_version_info():
    _, skipped = _placement()
    assert {f"{API}.core.v1.PersistentVolumeClaimList", f"{API}.apps.v1beta1.StatefulSet",
            "io.k8s.apimachinery.pkg.version.Info"} <= skipped


# ── oas2tosca --kubernetes ────────────────────────────────────────────────────

@pytest.fixture
def converted(tmp_path):
    spec = tmp_path / "swagger.json"
    spec.write_text(json.dumps(SPEC))
    out = tmp_path / "out"
    assert main_oas(["--kubernetes", str(spec), "--profile", "io.kubernetes:1.35", "-o", str(out)]) == 0
    return out


def _load(out, unit):
    return yaml.safe_load((out / f"{unit}.yaml").read_text())


def test_kubernetes_writes_one_file_per_unit_and_a_profile(converted):
    names = sorted(p.name for p in converted.iterdir())
    assert names == sorted(["TOSCA.meta", "profile.yaml", "apps.yaml", "autoscaling.yaml",
                            "core.yaml", "dra.yaml", "meta.yaml", "resource.yaml"])
    profile = yaml.safe_load((converted / "profile.yaml").read_text())
    assert profile["profile"] == "io.kubernetes:1.35"
    imports = profile["imports"]
    assert imports.index("meta.yaml") < imports.index("core.yaml") < imports.index("apps.yaml")


def test_kubernetes_resource_node_type(converted):
    sts = _load(converted, "apps")["node_types"]["StatefulSet"]
    assert sts["metadata"] == {GVK: {"group": "apps", "kind": "StatefulSet", "version": "v1"}}
    assert set(sts["properties"]) == {"metadata", "spec"}
    assert sts["attributes"]["status"]["type"] == "StatefulSetStatus"
    assert "capabilities" not in sts and "requirements" not in sts


def test_kubernetes_core_group_is_empty_string(converted):
    text = (converted / "core.yaml").read_text()
    assert "group: ''" in text


def test_kubernetes_by_value_companion(converted):
    apps = _load(converted, "apps")
    vct = apps["data_types"]["StatefulSetSpec"]["properties"]["volumeClaimTemplates"]
    assert vct["entry_schema"]["type"] == "core:PersistentVolumeClaimValue"
    assert vct["metadata"] == {"x-kubernetes-list-type": "atomic"}
    core = _load(converted, "core")
    assert "PersistentVolumeClaimValue" in core["data_types"]
    assert "PersistentVolumeClaim" in core["node_types"]


def test_kubernetes_action_request_is_a_data_type(converted):
    core = _load(converted, "core")
    assert "Binding" in core["data_types"]
    assert "Binding" not in core["node_types"]


def test_kubernetes_no_redfish_conventions(converted):
    for path in converted.glob("*.yaml"):
        doc = yaml.safe_load(path.read_text())
        assert all(i.get("url") != "Core.yaml" for i in doc.get("imports") or [] if isinstance(i, dict))
    assert not list(converted.glob("*_caps.yaml"))


def test_kubernetes_requires_value_model(tmp_path, capsys):
    spec = tmp_path / "swagger.json"
    spec.write_text(json.dumps(SPEC))
    rc = main_oas(["--kubernetes", "--relationship-model", "links", str(spec), "-o", str(tmp_path / "o")])
    assert rc == 1
    assert "value relationship model" in capsys.readouterr().err


def test_kubernetes_allows_references_to_skipped_alpha_types(tmp_path, capsys):
    # A GA type referencing an alpha type the rules skip (as batch/v1 does in 1.37).
    spec = copy.deepcopy(SPEC)
    spec["definitions"][f"{API}.batch.v1.JobSchedulingConfiguration"] = _obj(
        policy=_ref(f"{API}.scheduling.v1alpha3.WorkloadPodGroupSchedulingPolicy"))
    spec["definitions"][f"{API}.scheduling.v1alpha3.WorkloadPodGroupSchedulingPolicy"] = _obj(
        mode={"type": "string"})
    path = tmp_path / "swagger.json"
    path.write_text(json.dumps(spec))
    out = tmp_path / "out"
    assert main_oas(["--kubernetes", str(path), "-o", str(out)]) == 0
    assert "WorkloadPodGroupSchedulingPolicy" in capsys.readouterr().err
    batch = _load(out, "batch")
    assert batch["data_types"]["JobSchedulingConfiguration"]["properties"]["policy"]["type"] == "map"
