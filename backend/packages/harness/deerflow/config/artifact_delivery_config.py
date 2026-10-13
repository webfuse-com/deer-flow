"""[argus patch #133] What a run does with outputs it produced but did not present."""

from pydantic import BaseModel, Field


class ArtifactDeliveryConfig(BaseModel):
    """Run-end handling of produced but unpresented outputs."""

    auto_present: bool = Field(
        default=False,
        description=(
            "When a run created or changed files under /mnt/user-data/outputs and presented none of them, "
            "present them itself (the same artifacts update present_files makes) and finish the run as a "
            "success, instead of failing it with 'Artifact delivery incomplete'. Default false matches upstream."
        ),
    )
    max_paths: int = Field(
        default=20,
        ge=1,
        description="Most files auto_present adds in one run; a larger set is not presented and the run fails as before.",
    )
