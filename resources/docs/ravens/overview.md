# RAVENS Density & MuSIC

## Links

- **Paper (RAVENS):** [Voxel-Based Morphometry Using the RAVENS Maps: Methods and Validation Using Simulated Longitudinal Atrophy](https://pubmed.ncbi.nlm.nih.gov/11707092/) (Davatzikos et al., *NeuroImage*, 2001)
- **Registration:** [FireANTs](https://github.com/rohitrango/FireANTs) (GPU)
- **GitHub:** not yet publicly available

RAVENS (Regional Analysis of Volumes Examined in Normalized Space) is a voxel-based morphometry method. Each brain is deformed to a common template in a way that preserves tissue volume, so the intensity of the resulting map at each point reflects how much tissue the subject had in that region.

This pipeline runs DLMUSE, deformably registers each skull-stripped T1 scan to the iSTAGING template on the GPU (FireANTs), computes gray matter RAVENS maps, and summarizes them as opNMF/MuSIC regional values (64 components).

## What this pipeline produces

- **Gray matter RAVENS map** (`ravens_out/{MRID}_Label_GM_RAVENS.nii.gz`) — one per subject, in template space.
- **Warped T1** (`ravens_out/{MRID}_Warped.nii.gz`) — the subject's T1 registered to the template.
- **MuSIC regional values CSV** (`ravens_out/RAVENS_MUSIC_C64.csv`) — one row per subject, one column per component.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- No participants.csv is required. The pipeline runs DLMUSE segmentation internally.
- Requires a GPU.
