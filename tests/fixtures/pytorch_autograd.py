import json

import torch

torch.set_num_threads(2)
x = torch.arange(65536, dtype=torch.float64, device="cpu", requires_grad=True)
loss = x.square().sum()
loss.backward()
result = {
    "loss": loss.item(),
    "gradient": x.grad.tolist(),
    "threads": torch.get_num_threads(),
}
print("PYTORCH_RESULT=" + json.dumps(result))
