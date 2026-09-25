"""Pinny model packages: train on the website, run in the QC app.

```python
from pinny.model import ModelPackage, train_model, promotion_check

trained = train_model(name="receptacles", version="3", symbol_class="duplex",
                      original_template=tpl, positive_crops=pos, negative_crops=neg,
                      review_pairs=pairs)
trained.package.save("receptacles-3.pinny", signing_key=key)

model = ModelPackage.load("receptacles-3.pinny", verify_key=key)   # QC app
result = model.detect(page)            # canonical RGB raster, contracts §2
for d in result.accepted: ...
```

See ``docs/model-package.md`` for the file format.
"""

from .format import CONTRACTS_VERSION, FILE_EXTENSION, FORMAT, FORMAT_VERSION, ModelPackageError
from .package import Decision, ModelDetection, ModelPackage, ModelResult, PackageTemplate
from .promote import PromotionDecision, promotion_check
from .train import TrainingResult, train_model

__all__ = [
    "CONTRACTS_VERSION",
    "Decision",
    "FILE_EXTENSION",
    "FORMAT",
    "FORMAT_VERSION",
    "ModelDetection",
    "ModelPackage",
    "ModelPackageError",
    "ModelResult",
    "PackageTemplate",
    "PromotionDecision",
    "TrainingResult",
    "promotion_check",
    "train_model",
]
