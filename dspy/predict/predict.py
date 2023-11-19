import dsp
import random
import dspy

from dspy.predict.parameter import Parameter
from dspy.primitives.prediction import Prediction
from dspy.signatures.field import InputField, OutputField
from dspy.signatures.signature import infer_prefix
from datetime import datetime
from langfuse.model import InitialGeneration


class Predict(Parameter):
    def __init__(self, signature, **config):
        self.stage = random.randbytes(8).hex()
        self.signature = signature #.signature
        self.config = config
        if dspy.settings.langfuse.langfuse_client:
            print("predict.__init__: creating new trace")
            dspy.settings.langfuse.create_new_trace(reset_in_context=True)
        self.reset()

        # if the signature is a string
        if isinstance(signature, str):
            inputs, outputs = signature.split("->")
            inputs, outputs = inputs.split(","), outputs.split(",")
            inputs, outputs = [field.strip() for field in inputs], [field.strip() for field in outputs]

            assert all(len(field.split()) == 1 for field in (inputs + outputs))

            inputs_ = ', '.join([f"`{field}`" for field in inputs])
            outputs_ = ', '.join([f"`{field}`" for field in outputs])

            instructions = f"""Given the fields {inputs_}, produce the fields {outputs_}."""

            inputs = {k: InputField() for k in inputs}
            outputs = {k: OutputField() for k in outputs}

            for k, v in inputs.items():
                v.finalize(k, infer_prefix(k))
            
            for k, v in outputs.items():
                v.finalize(k, infer_prefix(k))

            self.signature = dsp.Template(instructions, **inputs, **outputs)

    
    def reset(self):
        self.lm = None
        self.traces = []
        self.train = []
        self.demos = []

    def dump_state(self):
        state_keys = ["lm", "traces", "train", "demos"]
        return {k: getattr(self, k) for k in state_keys}

    def load_state(self, state):
        for name, value in state.items():
            setattr(self, name, value)

        import dspy
        self.demos = [dspy.Example(**x) for x in self.demos]
    
    def __call__(self, **kwargs):
        # trace events from same context should not be added to different context
        if dspy.settings.langfuse.langfuse_client and not dspy.settings.langfuse.langfuse_in_context_call:
            print("predict.__call__: creating new trace")
            dspy.settings.langfuse.create_new_trace(reset_in_context=False)
        return self.forward(**kwargs)
    
    def forward(self, **kwargs):
        generationStartTime = datetime.now()
        
        # Extract the three privileged keyword arguments.
        signature = kwargs.pop("signature", self.signature)
        demos = kwargs.pop("demos", self.demos)
        config = dict(**self.config, **kwargs.pop("config", {}))

        # Get the right LM to use.
        lm = kwargs.pop("lm", self.lm) or dsp.settings.lm

        # If temperature is 0.0 but its n > 1, set temperature to 0.7.
        temperature = config.get("temperature", None)
        temperature = lm.kwargs['temperature'] if temperature is None else temperature

        num_generations = config.get("n", None)
        num_generations = lm.kwargs['n'] if num_generations is None else num_generations

        if (temperature is None or temperature <= 0.15) and num_generations > 1:
            config["temperature"] = 0.7
            # print(f"#> Setting temperature to 0.7 since n={num_generations} and prior temperature={temperature}.")

        # All of the other kwargs are presumed to fit a prefix of the signature.

        x = dsp.Example(demos=demos, **kwargs)

        if self.lm is None:
            x, C = dsp.generate(signature, **config)(x, stage=self.stage)
        else:
            with dsp.settings.context(lm=self.lm, query_only=True):
                # print(f"using lm = {self.lm} !")
                x, C = dsp.generate(signature, **config)(x, stage=self.stage)

        completions = []

        for c in C:
            completions.append({})
            for field in signature.fields:
                if field.output_variable not in kwargs.keys():
                    completions[-1][field.output_variable] = getattr(c, field.output_variable)

        pred = Prediction.from_completions(completions, signature=signature)
            
        if dsp.settings.langfuse:
            _ = dspy.settings.langfuse.langfuse_trace.generation(InitialGeneration(
                name=lm.kwargs["model"],
                startTime=generationStartTime,
                endTime=datetime.now(),
                model=lm.kwargs["model"],
                modelParameters=lm.kwargs,
                prompt=lm.history[-1]['prompt'],
                completion=pred.toDict(),
                metadata=kwargs
            ))
            # TODO: We need to integrate this somewhere to prepare for termination
            # but this is a blocking call, so we have to be careful.
            # langfuse.flush()
        if dsp.settings.trace:
            trace = dsp.settings.trace
            trace.append((self, {**kwargs}, pred))
        return pred

    def __repr__(self):
        return f"{self.__class__.__name__}({self.signature})"



# TODO: get some defaults during init from the context window?
# # TODO: FIXME: Hmm, I guess expected behavior is that contexts can
# affect exeuction. Well, we need to determine whether context dominates, __init__ demoninates, or forward dominates.
# Generally, unless overwritten, we'd see n=None, temperature=None.
# That will eventually mean we have to learn them.
