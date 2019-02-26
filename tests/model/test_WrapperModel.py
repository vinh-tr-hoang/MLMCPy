import pytest

from MLMCPy.model import WrapperModel

class TestCostClass:

    def __init__(self, cost=10):
        self.cost = cost

def test_attach_model_exception():
    cost_class = TestCostClass()
    wrapper_model = WrapperModel()

    with pytest.raises(NotImplementedError):
        wrapper_model.attach_model(cost_class)