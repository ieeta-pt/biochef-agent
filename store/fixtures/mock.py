"""The data of the mockups, as corrected in docs/agent-ui/SPEC.md section 0.1.

Every name, id and key here is invented. The documents follow the models of
ieeta-pt/biochef-coordinator#6, closely enough to exercise the store. They are
not signed, and member public keys are generated when the seed runs.

Times are offsets from the moment of seeding, so that the 24 hour views stay
filled however long ago the database was made.
"""

from datetime import timedelta

CARDIO = "cardio-pt"
RARE = "rare-variants-pilot"
SEPSIS = "sepsis-biomarkers"
MICROBIOME = "microbiome-pilot"

# Name, as the token's name claim carries it: (member id, ls_aai_id). The ids are
# opaque, as they are in a real study. A name reaches the store only through a
# token's claims, so it appears in no study document and no request.
MEMBERS = {
    "Ana Costa": ("m-01", "a3f1c09e7b2d4e58@lifescience-ri.eu"),
    "Rui Matos": ("m-02", "7c44b1d09ea2f613@lifescience-ri.eu"),
    "Sofia Lima": ("m-03", "e90d25c871ab3f46@lifescience-ri.eu"),
    "Marta Reis": ("m-04", "15bf7ae3c06d9248@lifescience-ri.eu"),
    "Paulo Neves": ("m-05", "c2e86f10d7b34a95@lifescience-ri.eu"),
}

NODE = "Example TRE (test)"
NODES = (NODE, "Second TRE", "Third TRE")

CARDIO_DATASET = "EGAD00001009912"
SEPSIS_DATASET = "EGAD00001008830"
MICROBIOME_DATASET = "EGAD00001007115"
RARE_DATASET = "EGAD00001010247"

CARDIO_GOAL = "CARDIO-PT cohort\nAssociation of variants with cardiac outcomes."

RULES_V2 = {"aggregates_only": True, "min_count": 10,
            "types": ["tsv", "png"], "max_bytes": 10_000_000}
RULES_V3 = {**RULES_V2, "min_count": 5}


def days(count):
    return timedelta(days=count)


def hours(count):
    return timedelta(hours=count)


# (study, version, goal, proposer, members, dataset, nodes, rules, received,
#  state). Listed oldest first: a version's previous digest is the one above it.
STUDY_VERSIONS = (
    (CARDIO, 1, CARDIO_GOAL, "Ana Costa", ("Ana Costa",),
     CARDIO_DATASET, NODES, RULES_V2, days(30), "superseded"),
    (CARDIO, 2, CARDIO_GOAL, "Ana Costa", ("Ana Costa", "Rui Matos"),
     CARDIO_DATASET, NODES, RULES_V2, days(26), "approved"),
    (CARDIO, 3, CARDIO_GOAL, "Ana Costa",
     ("Ana Costa", "Rui Matos", "Sofia Lima"),
     CARDIO_DATASET, NODES, RULES_V3, hours(2), "pending"),
    (RARE, 1, "Rare variants pilot", "Marta Reis", ("Marta Reis", "Paulo Neves"),
     RARE_DATASET, NODES[:2], RULES_V2, hours(20), "pending"),
    (SEPSIS, 1, "Sepsis biomarkers", "Marta Reis", ("Marta Reis", "Paulo Neves"),
     SEPSIS_DATASET, NODES[:2], RULES_V2, days(20), "approved"),
    (MICROBIOME, 1, "Microbiome pilot", "Rui Matos", ("Rui Matos",),
     MICROBIOME_DATASET, NODES[:2], RULES_V2, days(25), "withdrawn"),
)

UNVERIFIED = {
    (RARE, 1, "Paulo Neves"):
        "The keys listed are not the ones this LS Login sign in vouches for",
}

# (id, tool, description, parameter values). A step's values come from here,
# except that None takes the workflow's own, which is what tells its variants
# apart.
GWAS_STEPS = (
    ("qc_samples", "plink2", "Removes samples with high missingness.",
     {"mind": "0.05"}),
    ("qc_variants", "bcftools", "Keeps variants above the MAF and HWE thresholds.",
     None),
    ("assoc", "regenie", "Association test against the phenotype.",
     {"phenotype": "ldl"}),
    ("summarise", "biochef-report", "Aggregate table and Manhattan plot.",
     {"top": "100"}),
)
COHORT_STEPS = (
    ("summarise", "biochef-report", "Aggregate table by group.", None),
)

TOOL_DIGESTS = {
    "plink2": ("a1d9", "3e02"),
    "bcftools": ("77c4", "b1aa"),
    "regenie": ("c3b8", "5f61"),
    "biochef-report": ("19fa", "d7c0"),
}
TOOL_CHANGES = {"gwas-140": {"bcftools": ("0e5f", "92dd")}}
"""What differs from TOOL_DIGESTS: the one tool 1.4.0 changed."""

# (key, study, version, name, workflow version, digest ends, parameter values,
#  state, received). `key` is how the requests below refer to a workflow.
WORKFLOWS = (
    ("gwas-maf01", CARDIO, 2, "gwas-qc", "1.3.2", ("2a77", "c5e8"),
     {"maf_min": "0.01", "hwe_p": "1e-6"}, "approved", days(24)),
    ("gwas-maf05", CARDIO, 2, "gwas-qc", "1.3.2", ("6d02", "a1f3"),
     {"maf_min": "0.05", "hwe_p": "1e-6"}, "approved", days(24)),
    ("cohort", CARDIO, 2, "cohort-summary", "0.2.0", ("f09d", "1b44"),
     {"group_by": "sex,age_band"}, "approved", days(22)),
    ("gwas-140", CARDIO, 2, "gwas-qc", "1.4.0", ("4be1", "90c3"),
     {"maf_min": "0.01", "hwe_p": "1e-6"}, "pending", hours(27)),
    ("sepsis-cohort", SEPSIS, 1, "cohort-summary", "0.2.0", ("c7d1", "03fa"),
     {"group_by": "sex,age_band"}, "approved", days(18)),
)

# (id, study, version, workflow key, member, received, verdict, failed check,
#  joined request, run). Mock 04 lists the first six, mock 02 adds the last two.
REQUESTS = (
    ("req_8e90", SEPSIS, 1, "sepsis-cohort", "Marta Reis", hours(1), "accepted",
     None, None, "run_8e90"),
    ("req_8f40", CARDIO, 2, "gwas-140", "Rui Matos", hours(2), "refused",
     "workflow_not_approved", None, None),
    ("req_8f33", SEPSIS, 1, "sepsis-cohort", "Paulo Neves", hours(3), "refused",
     "visa_missing", None, None),
    ("req_8f22", CARDIO, 2, "gwas-maf01", "Rui Matos", hours(5), "joined", None,
     "req_8f21", "run_8f21"),
    ("req_8f21", CARDIO, 2, "gwas-maf01", "Rui Matos", hours(6), "accepted",
     None, None, "run_8f21"),
    ("req_7c03", CARDIO, 2, "gwas-maf01", "Ana Costa", hours(20), "accepted",
     None, None, "run_7c03"),
    ("req_7b88", CARDIO, 2, "cohort", "Ana Costa", days(4), "accepted", None,
     None, "run_7b88"),
    ("req_7a10", CARDIO, 2, "gwas-maf01", "Rui Matos", days(5), "refused",
     "expired", None, None),
)

STUDY_REQUEST_COUNTS = {CARDIO: 14, SEPSIS: 6}
"""The counts on mock 02. Older requests are generated up to these."""

BINDINGS = (
    (CARDIO_DATASET, "config", "cardio/2019_2024/", "config file",
     f"https://dac.example.org/datasets/{CARDIO_DATASET}"),
    (SEPSIS_DATASET, "config", "sepsis/icu_v4/", "config file",
     f"https://dac.example.org/datasets/{SEPSIS_DATASET}"),
    (MICROBIOME_DATASET, "page", "micro/16s_2023/", "j.silva", None),
)

OUTPUT_FILES = (
    {"file": "assoc_summary.tsv", "size": 412_000, "verdict": "pass",
     "reason": "aggregate, smallest cell 23"},
    {"file": "manhattan.png", "size": 1_800_000, "verdict": "pass",
     "reason": "allowed type"},
    {"file": "qc_log.txt", "size": 96_000, "verdict": "held_back",
     "reason": "type not allowed by the study"},
)

# Step durations of run_8f21, in minutes.
STEP_MINUTES = {"qc_samples": 9, "qc_variants": 31, "assoc": 47, "summarise": 4}

FEDERATION_VERSION = 14
FEDERATION_NODES = NODES + ("Fourth TRE", "Fifth TRE")
