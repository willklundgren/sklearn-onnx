# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
import numpy as np
from ..proto import TensorProto
from ..common._topology import Variable, Scope
from ..common._container import ModelComponentContainer
from ..common import utils
from ..proto import get_latest_tested_opset_version, onnx_proto
from ..proto.onnx_helper_modified import make_graph, make_model
from ..helpers.onnx_helper import infer_outputs
from .graph_state import GraphState
from .type_helper import _guess_type


class OnnxOperatorItem:
    """
    Accessor to one of the output returned by a *OnnxOperator*.

    :param onx_op: OnnxOperator
    :param index: integer
    """
    def __init__(self, onx_op, index):
        if not isinstance(index, int):
            raise TypeError("index must be an integer.")
        self.onx_op = onx_op
        self.index = index

    def add_to(self, scope, container, operator=None):
        """
        Adds outputs to the container if not already added,
        registered the outputs if the node is not final.

        :param scope: scope
        :param container: container
        :param operator: overwrite inputs
        """
        self.onx_op.add_to(scope, container, operator=operator)

    def get_output(self, i=0):
        """
        Returns the output.
        """
        if i != 0:
            raise IndexError("Can only return the first item.")
        return self.onx_op.get_output(self.index)

    @property
    def outputs(self):
        """
        Returns the outputs of the node.
        """
        return self.onx_op.outputs[self.index:self.index + 1]


class OnnxOperator:
    """
    Ancestor to every *ONNX* operator exposed in
    :mod:`onnx_ops` and :mod:`onnx_ops_ml`. These files
    are automatically generated by unit test
    *test_onnx_operators_parse_spec*
    Every instance is supposed to be included in
    a graph as a node.

    :param inputs: list of inputs expected by the operator
    :param op_version: to select a specific version of the operator
    :param output_names: used defined names for the outputs
    :param domain: to overwrite the default domain
    :param kwargs: additional parameters of the operator
    """
    class OnnxOperatorVariable:

        def __init__(self, index, name=None):
            self.index = index
            self.name = name

        def __repr__(self):
            return "OnnxOperatorVariable('%s')" % self.name

    class UnscopedVariable:
        def __init__(self, name):
            self.name = name

        def __eq__(self, name):
            if isinstance(name, str):
                return name == self.name
            elif isinstance(name, OnnxOperator.UnscopedVariable):
                return self.name == name.name
            else:
                raise TypeError('Unsupported type for comparison {}'.format(
                    type(name)))

        def __repr__(self):
            return "UnscopedVariable('%s')" % self.name

    class ConstantVariable:
        def __init__(self, value, implicit_cast=True):
            self.value = value
            self.implicit_cast = implicit_cast

        @property
        def ConstantValue(self):
            return self.value

        @property
        def ImplicitCast(self):
            return self.implicit_cast

    def find_schema(self, op_version):
        """
        Checks if there is an existing schema for a
        specific version.

        :param op_version: requested version
        :return: schema
        """
        if not hasattr(self.__class__, 'past_version'):
            raise RuntimeError("Missing attribute 'past_version', there is "
                               "no other available schema.")
        found = None
        for v in self.past_version.values():
            if v.since_version > op_version:
                continue
            if found is None or v.since_version > found.since_version:
                found = v
        if found is None:
            raise RuntimeError(
                "Operator '{}': requested version {} < "
                "{} schema version.".format(
                    self.__class__.__name__,
                    op_version, self.since_version))
        return found

    def __init__(self, *inputs, op_version=None, output_names=None,
                 domain=None, **kwargs):

        if output_names is None and self.__class__.__name__ in {
                "OnnxScan"}:
            raise NotImplementedError(
                "The class cannot infer the number of variables "
                "for node '{}' yet. output_names must be specified"
                ".".format(self.__class__.__name__))

        self.op_version = op_version or get_latest_tested_opset_version()
        self.since_version = self.__class__.since_version

        if self.op_version < self.since_version:
            schema = self.find_schema(self.op_version)
            self.since_version = schema.since_version
            self.expected_inputs = schema.expected_inputs
            self.expected_outputs = schema.expected_outputs
            self.input_range = schema.input_range
            self.output_range = schema.output_range
        else:
            self.expected_inputs = self.__class__.expected_inputs
            self.expected_outputs = self.__class__.expected_outputs
            self.input_range = self.__class__.input_range
            self.output_range = self.__class__.output_range

        if self.op_version < self.since_version:
            raise RuntimeError(
                "Operator '{}': requested version {} < "
                "{} schema version.".format(
                    self.__class__.__name__,
                    self.op_version, self.since_version))

        self.state = None
        self.domain = domain
        self.kwargs = kwargs
        self.onnx_prefix_name = None

        # check inputs
        if len(inputs) == 0:
            if self.input_range[0] == self.input_range[1]:
                self.inputs = [_[0] for _ in self.expected_inputs]
            else:
                # The number of inputs may vary.
                self.inputs = None
        else:
            self.inputs = []
            for inp in inputs:
                if isinstance(inp, str):
                    self.inputs.append(OnnxOperator.UnscopedVariable(inp))
                elif isinstance(inp, (OnnxOperator, Variable,
                                      OnnxOperatorItem)):
                    self.inputs.append(inp)
                elif isinstance(inp, np.ndarray):
                    self.inputs.append(
                        OnnxOperator.ConstantVariable(
                            inp, implicit_cast=True))
                elif isinstance(inp, TensorProto):
                    self.inputs.append(OnnxOperator.ConstantVariable(inp))
                elif isinstance(inp, (OnnxOperator.OnnxOperatorVariable,
                                      OnnxOperator.ConstantVariable)):
                    self.inputs.append(inp)
                elif isinstance(inp, (np.int64, np.float32,
                                      np.float64, np.bool)):
                    self.inputs.append(inp)
                elif isinstance(inp, (float, )):
                    self.inputs.append(np.float64(inp))
                elif isinstance(inp, (int, )):
                    self.inputs.append(np.int64(inp))
                else:
                    raise TypeError("Unable to interpret the "
                                    "input name for type {} in "
                                    "operator '{}'.".format(
                                        type(inp), self.__class__.__name__))

        if self.inputs is not None:
            if (len(self.inputs) < self.input_range[0] or
                    len(self.inputs) > self.input_range[1]):
                raise RuntimeError("Operator '{}' expects a number of inputs "
                                   "in [{}, {}] not {}".format(
                                       self.operator_name,
                                       *self.input_range,
                                       len(self.inputs)))

        # check output
        if (hasattr(output_names, 'outputs') and
                output_names.outputs is not None):
            self.output_names = [out.full_name
                                 for out in output_names.outputs]
        else:
            self.output_names = output_names
        if self.output_names:
            for i in range(len(self.output_names)):
                name = self.output_names[i]
                if isinstance(name, Variable):
                    self.output_names[i] = name.onnx_name
                elif not isinstance(name, str):
                    raise TypeError("output_names must be a list of strings "
                                    "and element {} is {}".format(
                                        i, type(name)))

    def set_onnx_name_prefix(self, onnx_prefix_name):
        """
        Provides a name to define a prefix in the onnx graph
        to avoid to get unreadable node names. The method
        does not overwrite an existing name, it propagates
        the prefix to inputs and stops the propagation
        if the prefix is already defined.
        """
        if self.onnx_prefix_name is None:
            self.onnx_prefix_name = onnx_prefix_name
            for inp in self.inputs:
                if hasattr(inp, 'onnx_prefix_name'):
                    inp.set_onnx_name_prefix(onnx_prefix_name)

    @property
    def onnx_prefix(self):
        if self.onnx_prefix_name is None:
            name = self.__class__.__name__
            if name.startswith("Onnx"):
                name = name[4:]
            return name[:2]
        else:
            return self.onnx_prefix_name

    def __getitem__(self, index):
        """
        Returns an accessor to one of the output
        of this node.
        """
        return OnnxOperatorItem(self, index)

    def get_output(self, i):
        """
        Returns the ith output.
        """
        if hasattr(self, 'output_names_'):
            return self.output_names_[i]
        if (self.output_names and i < len(self.output_names) and
                self.output_names[i]):
            return self.output_names[i]
        if i < len(self.expected_outputs):
            return self.expected_outputs[i][0]
        elif i < self.output_range[1]:
            if i > 1000:
                raise IndexError("You should redesign your operator.")
            return "O%d" % i
        else:
            raise IndexError("Output %d does not exist." % i)

    def update_name(self, i, name):
        """
        Updates the name of a variable after it was scoped.
        """
        if hasattr(self, 'output_names_') and i < len(self.output_names_):
            if self.output_names_[i] != name:
                raise RuntimeError("Inconsistent, cannot "
                                   "changed variable name "
                                   "after it was used: "
                                   "'{}' != '{}'".format(
                                       self.output_names_[i],
                                       name))
        if self.output_names is None:
            self.output_names = []
        while len(self.output_names) <= i:
            self.output_names.append(None)
        self.output_names[i] = name

    def add_to(self, scope, container, operator=None):
        """
        Adds outputs to the container if not already added,
        registered the outputs if the node is not final.

        :param scope: scope
        :param container: container
        :param operator: overwrite inputs
        """
        if self.state is None:
            if self.is_deprecated:
                raise RuntimeError("Node '{}' is deprecated. "
                                   "This API cannot deprecated nodes."
                                   "".format(self.__class__.__name__))
            if (self.op_version is not None and
                    self.op_version < self.since_version):
                raise RuntimeError("Incompatible versions for node '{}' "
                                   "op_version {} < since_version {}."
                                   "".format(self.__class__.__name__,
                                             self.op_version,
                                             self.since_version))
            if self.kwargs.get('op_version', '') is None:
                kwargs = self.kwargs.copy()
                del kwargs['op_version']
            else:
                kwargs = self.kwargs

            if hasattr(self, 'output_names_'):
                outputs = self.output_names_
            elif self.output_names:
                if not isinstance(self.output_names, (list, tuple)):
                    louts = [self.output_names]
                else:
                    louts = self.output_names
                outputs = []
                for name in louts:
                    if name.startswith('u(') and name[-1] == ')':
                        name = scope.get_unique_variable_name(name[2:-1])
                    outputs.append(name)
                self.output_names_ = outputs
            else:
                outputs = []
                for name in self.expected_outputs:
                    name = scope.get_unique_variable_name(
                        self.onnx_prefix + "_" + name[0])
                    outputs.append(name)
                self.output_names_ = outputs

            domain = self.domain
            if domain is None:
                domain = self.__class__.domain
            inputs = []
            for input in self.inputs:
                if isinstance(input, OnnxOperator.OnnxOperatorVariable):
                    if operator is None:
                        raise RuntimeError("A placeholder cannot be replaced "
                                           "as an operator is not specified.")
                    if len(operator.inputs) == 0:
                        raise RuntimeError("No input variable in {}.".format(
                            operator))
                    # The inputs must be looked into the graph.
                    for i in operator.inputs:
                        if i.raw_name == input.name:
                            inputs.append(i)
                            break
                    else:
                        vars = ', '.join(map(lambda o: "'%s'" % o.raw_name,
                                             operator.inputs))
                        raise RuntimeError("Unable to find variable "
                                           "{} in {}.".format(input, vars))
                else:
                    inputs.append(input)
            self.state = GraphState(
                inputs, self.output_names_, self.operator_name,
                scope, container, None, op_version=self.op_version,
                op_domain=domain, onnx_prefix_name=self.onnx_prefix,
                **self.kwargs)
            self.state.run(operator=operator)

    @property
    def outputs(self):
        """
        Returns the outputs of the node.
        """
        if self.state is None:
            raise RuntimeError("Method add_to was not called.")
        return self.state.outputs

    def _clean_attributes(self, *args, recursive=True):
        """
        Removes attributes in this node and its parents.
        """
        for arg in args:
            if arg == 'state':
                self.state = None
            elif hasattr(self, arg):
                delattr(self, arg)
        if recursive:
            for obj in self.inputs:
                if isinstance(obj, OnnxOperator):
                    obj._clean_attributes(*args, recursive=True)

    def to_onnx(self, inputs=None, outputs=None, other_outputs=None,
                dtype=np.float32, target_opset=None, domain=None):
        """
        Converts this operator into an ONNX graph.

        :param inputs: specific inputs (as a dictionary) or
            default inputs if not specified
        :param outputs: specific outputs
        :param other_outputs: additional outputs to consider
            as graph outputs but not outputs of this particular
            node
        :param dtype: force the use of a specific float type,
            either `np.float32` or `np.float64`, it must be specified
        :param target_opset: target opset, None for the default one
        :param domain: domain of the operator
        """
        if (self.op_version is not None and target_opset is not None and
                self.op_version > target_opset):
            raise RuntimeError(
                "target_opset={} is lower than the version={} requested "
                "for this node '{}'.".format(
                    target_opset, self.op_version, self.__class__.__name__))
        if hasattr(self, "state"):
            # The conversion already happened and needs to be cleaned.
            self._clean_attributes("output_names_", "state")
        if inputs is None:
            raise NotImplementedError("inputs must be specified.")
        if isinstance(inputs, dict):
            inputs = [(k, v) for k, v in inputs.items()]
        new_inputs = []
        for obj in inputs:
            if isinstance(obj, Variable):
                new_inputs.append((obj.onnx_name, obj.type))
            elif isinstance(obj, tuple) and len(obj) == 2:
                ty = _guess_type(obj[1])
                new_inputs.append((obj[0], ty))
            else:
                raise TypeError("Unexpected type {}.".format(type(obj)))
        inputs = new_inputs
        for name, typ in inputs:
            if typ is None:
                raise RuntimeError("Type input '{}' for operator '{}' "
                                   "is unknown. You should specify "
                                   "input types.".format(
                                       name, self.__class__.__name__))

        if target_opset is None:
            target_opset = get_latest_tested_opset_version()
        container = ModelComponentContainer(target_opset, dtype=dtype)
        if container.target_opset < 9 and self.domain in ('', None):
            raise RuntimeError("The operator cannot be converted into ONNX."
                               " It requires ONNX op_set >= 9 (={}, "
                               "name='{}', domain='{}')"
                               ".".format(container.target_opset,
                                          self.__class__.__name__,
                                          self.domain))
        model_name = self.__class__.__name__
        scope = Scope(model_name, target_opset=target_opset,
                      variable_name_set=set(_[0] for _ in inputs))
        for inp in inputs:
            container.add_input(Variable(inp[0], inp[0],
                                         scope=scope, type=inp[1]))
        self.add_to(scope, container)
        if other_outputs is not None:
            for out in other_outputs:
                if not hasattr(out, 'add_to'):
                    raise RuntimeError(
                        "Extra outputs must have method 'add_to'.")
                out.add_to(scope, container)

        # infer shapes
        if outputs:
            shapes = []
            for o in outputs:
                if isinstance(o, Variable):
                    shapes.append(o)
                elif isinstance(o, tuple):
                    shapes.append(Variable(o[0], o[0], None, o[1]))
                else:
                    raise TypeError("Outputs must be Variable or "
                                    "tuple(name, type).")
        else:
            shapes = infer_outputs(container, container.inputs,
                                   initializer=container.initializers)

            if self.output_names:
                shapes = [shape for shape in shapes
                          if shape.onnx_name in self.output_names]

        # add the output to the container
        for shape in shapes:
            container.add_output(shape)

        # convert the graph
        graph = make_graph(
            container.nodes, model_name, container.inputs,
            container.outputs, container.initializers)
        onnx_model = make_model(graph)

        # domains
        domains = {}
        version = target_opset
        for n in container.nodes:
            domains[n.domain] = max(domains.get(n.domain, version),
                                    getattr(n, 'op_version', version))
        for i, (k, v) in enumerate(domains.items()):
            if i == 0 and len(onnx_model.opset_import) == 1:
                op_set = onnx_model.opset_import[0]
            else:
                op_set = onnx_model.opset_import.add()
            op_set.domain = k
            op_set.version = domains.get(k, version)

        # metadata
        onnx_model.ir_version = onnx_proto.IR_VERSION
        onnx_model.producer_name = utils.get_producer()
        onnx_model.producer_version = utils.get_producer_version()
        onnx_model.domain = utils.get_domain()
        onnx_model.model_version = utils.get_model_version()

        return onnx_model

    def enumerate_nodes(self):
        """
        Iterates on all nodes of the graph.
        """
        yield self
        for input in self.inputs:
            if isinstance(input, OnnxOperator):
                for i in input.enumerate_nodes():
                    yield i

    def enumerate_variables(self):
        """
        Iterates on all nodes of the graph to find variables.
        Returns an iterator `(node, i)` which means
        `node.inputs[i]` is a variable.
        """
        for node in self.enumerate_nodes():
            if self.inputs:
                for i, input in enumerate(self.inputs):
                    if isinstance(input, (OnnxOperator.UnscopedVariable,
                                          Variable)):
                        yield (node, i)

    def enumerate_initial_types(self):
        """
        Retrieves iniatial types of the implemented functions.
        It goes through the graph and returns the name and types
        of all variables not computed by an intemediate node.

        :return: list of `(name, type)`
        """
        for node, i in self.enumerate_variables():
            input = node.inputs[i]
            if isinstance(input, Variable):
                yield (input.onnx_name, input.type)
            elif isinstance(input, OnnxOperator.UnscopedVariable):
                name = input.name
                typ = node.expected_inputs[i]
                yield (name, typ)
