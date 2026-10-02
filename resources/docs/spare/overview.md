# SPARE Brain Age & Disease Scores

## Links

- **Paper:** [Longitudinal progression of Alzheimer's-like patterns of atrophy in normal older adults: the SPARE-AD index](https://pubmed.ncbi.nlm.nih.gov/19416949/) (Davatzikos et al., *Brain*, 2009)
- **GitHub:** [CBICA/spare_score](https://github.com/CBICA/spare_score)
- **Documentation:** [spare_score docs](https://cbica.github.io/spare_score/)

SPARE (Spatial Pattern of Abnormalities for Recognition of Early disease) scores are machine-learning biomarkers derived from structural MRI features. Each SPARE score captures a distinct pattern of brain change associated with aging or a specific neurological condition, expressed as a continuous value relative to a normative reference population.

## Available score sets

**T1-only (SPARE-All)**  
Computed from DLMUSE regional brain volumes extracted from T1-weighted MRI alone. Includes:

- **SPARE-AD** — pattern associated with Alzheimer's disease neurodegeneration
- **SPARE-Age** — predicted brain age; the gap between SPARE-Age and chronological age (BAG) reflects accelerated or decelerated brain aging
- Additional disease-specific scores (see CSV column headers for the full list)

**T1 + FLAIR (SPARE-All CVM)**  
Adds white matter lesion features from DLWMLS to the T1-derived volumes, enabling cardiovascular and cerebrovascular disease-related scores (CVM variants).

## Harmonized variants

Harmonized variants use ComBat-FAM corrected DLMUSE volumes as input. Use harmonized SPARE scores when:
- Your data comes from multiple sites or scanner types
- You intend to combine your results with a reference cohort from a different acquisition protocol

Harmonized outputs are written to a separate directory (`ml_biomarkers_harmonized/`) and do not overwrite standard outputs.

## Input requirements

- DLMUSE regional volumes CSV (produced by the DLMUSE pipeline)
- `participants/participants.csv` with columns: `MRID`, `Age` (0–120), `Sex` (M or F)
- CVM variants additionally require DLWMLS outputs (T1 + FLAIR scans needed)
- Minimum 3 subjects required; 30+ recommended for reliable estimates
