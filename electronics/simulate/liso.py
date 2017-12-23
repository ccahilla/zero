"""LISO file parsing and running

This module provides classes to parse LISO input and output files and to run
native LISO binaries automatically. The input and output parsers implement
methods to search and identify components and commands in their respective
files.
"""

import sys
import os.path
import abc
import subprocess
import logging
import re
import numpy as np
from tempfile import NamedTemporaryFile

from ..data import (VoltageTransferFunction, CurrentTransferFunction,
                    NoiseSpectrum, Series, ComplexSeries)
from ..format import SIFormatter
from .circuit import Circuit
from .components import Component, Resistor, Capacitor, Inductor, OpAmp, Node
from .solution import Solution

LOGGER = logging.getLogger("liso")

class BaseParser(object, metaclass=abc.ABCMeta):
    COMMENT_REGEX = re.compile("^#.*?$")

    def __init__(self, filepath):
        """Instantiate a LISO parser

        :param filepath: path to LISO file
        :type filepath: str
        """

        # file to parse
        self.filepath = filepath

        # default circuit values
        self.frequencies = None
        self.output_nodes = set()
        self.circuit = Circuit()

        self._load_file()

    def add_output_node(self, node):
        self.output_nodes.add(node)

    def _load_file(self):
        """Load and parse from file"""

        with open(self.filepath, "r") as obj:
            self.parse_lines(obj.readlines())

    @abc.abstractmethod
    def parse_lines(self, lines):
        return NotImplemented

    @abc.abstractmethod
    def solution(self):
        """Get solution"""
        return NotImplemented

    def show(self):
        """Show LISO results"""

        if not self.calc_tfs and not self.calc_noise:
            LOGGER.warning("nothing to show")

        solution = self.solution()

        if self.calc_tfs:
            solution.plot_tf(output_nodes=list(self.output_nodes))

        if self.calc_noise:
            solution.plot_noise()

        # display plots
        solution.show()

    @classmethod
    def tokenise(cls, line):
        """Tokenise a LISO line

        :param line: line to tokenise
        :type line: str
        :return: tokens that make up each line
        :rtype: List[str]
        """

        # split into parts and remove extra whitespace
        return [line.strip() for line in line.split()]

    @property
    def calc_tfs(self):
        return len(self.output_nodes) > 0

    @property
    def calc_noise(self):
        return self.circuit.noise_node is not None

    def _add_lcr(self, _class, name, value, node1_name, node2_name):
        """Add new L, C or R component

        :param _class: component class to create
        :type _class: type
        :param name: component name
        :type name: str
        :param value: component value
        :type value: float
        :param node1_name: node 1 name
        :type node1_name: str
        :param node2_name: node 2 name
        :type node2_name: str
        :return: new component
        :rtype: :class:`~Component`
        """

        node1 = Node(node1_name)
        node2 = Node(node2_name)

        LOGGER.info("adding %s [%s = %s, in %s, out %s]",
                    _class.__name__.lower(), name, value, node1, node2)

        self.circuit.add_component(_class(name=name, value=value, node1=node1,
                                          node2=node2))

    def _add_resistor(self, *args, **kwargs):
        """Add resistor

        :return: new resistor
        :rtype: :class:`~Resistor`
        """

        self._add_lcr(Resistor, *args, **kwargs)

    def _add_capacitor(self, *args, **kwargs):
        """Add capacitor

        :return: new capacitor
        :rtype: :class:`~Capacitor`
        """

        self._add_lcr(Capacitor, *args, **kwargs)

    def _add_inductor(self, *args, **kwargs):
        """Add inductor

        :return: new inductor
        :rtype: :class:`~Inductor`
        """

        self._add_lcr(Inductor, *args, **kwargs)

    def _add_opamp(self, name, model, node1_name, node2_name, node3_name):
        """Add op-amp

        :return: new op-amp
        :rtype: :class:`~OpAmp`
        """

        # add nodes first
        node1 = Node(node1_name)
        node2 = Node(node2_name)
        node3 = Node(node3_name)

        LOGGER.info("adding op-amp [%s = %s, in+ %s, in- %s, out %s]",
                    name, model, node1, node2, node3)

        self.circuit.add_component(OpAmp(name=name, model=model, node1=node1,
                                         node2=node2, node3=node3))

class InputParser(BaseParser):
    COMPONENTS = ["r", "c", "l", "op"]
    DIRECTIVES = {"input_nodes": ["uinput", "vinput"],
                  "output_nodes": ["uoutput", "voutput"],
                  "noise_node": ["noise"],
                  "frequencies": ["freq"]}

    def __init__(self, *args, **kwargs):
        super(InputParser, self).__init__(*args, **kwargs)

    @property
    def directives(self):
        """Get sequence of supported directives

        :return: directives
        :rtype: Generator[str]
        """

        for directive_list in self.DIRECTIVES.values():
            yield from directive_list

    def parse_lines(self, lines):
        """Parses a list of LISO input file lines

        :param lines: lines to parse
        :type lines: Sequence[str]
        """

        # open file
        with open(self.filepath, "r") as obj:
            for tokens in [self.tokenise(line) for line in lines
                           if not line.startswith("#")]:
                self._parse_tokens(tokens)

    def _parse_tokens(self, tokens):
        """Parse LISO input file tokens as commands

        :param tokens: tokens that make up a LISO line
        :type tokens: Sequence[str]
        """

        # ignore empty lines
        if len(tokens) < 1:
            return

        command = tokens[0]

        if command in self.COMPONENTS:
            # this is a component
            self._parse_component(command, tokens[1:])
        elif command in self.directives:
            # this is a directive
            self._parse_directive(command, tokens[1:])

    def _parse_component(self, command, options):
        """Parse LISO tokens as component

        :param command: command string, e.g. "r" or "op"
        :type command: str
        :param options: tokens after command token
        :type options: Sequence[str]
        """

        if command == "r":
            self._add_resistor(*options)
        elif command == "c":
            self._add_capacitor(*options)
        elif command == "l":
            self._add_inductor(*options)
        elif command == "op":
            self._add_opamp(*options)
        else:
            raise ValueError("Unknown component: %s" % command)

    def _parse_directive(self, directive, options):
        """Parse LISO tokens as directive

        :param directive: directive string, e.g. "vinput"
        :type directive: str
        :param options: directive options
        :type options: Sequence[str]
        :raises ValueError: if directive is unknown
        """

        if directive in self.DIRECTIVES["input_nodes"]:
            self._parse_input_nodes(options)
        elif directive in self.DIRECTIVES["output_nodes"]:
            self._parse_output_nodes(options[0])
        elif directive in self.DIRECTIVES["noise_node"]:
            self._parse_noise_node(options[0])
        elif directive in self.DIRECTIVES["frequencies"]:
            self._parse_frequencies(options)
        else:
            raise ValueError("Unknown directive: %s" % directive)

    def _parse_input_nodes(self, node_options):
        """Parse LISO token as input node directive

        :param node_options: input node options
        :type node_options: Sequence[str]
        """

        # we always have at least a positive node
        self.circuit.input_node_p = Node(node_options[0])

        if len(node_options) > 3:
            # floating input
            self.circuit.input_node_m = Node(node_options[1])
            self.circuit.input_impedance = float(node_options[2])

            LOGGER.info("adding floating input nodes +%s, -%s with impedance "
                        " %f", self.circuit.input_node_p,
                        self.circuit.input_node_m, self.circuit.input_impedance)
        else:
            self.circuit.input_impedance = float(node_options[1])

            LOGGER.info("adding input node %s with impedance %s",
                        self.circuit.input_node_p,
                        SIFormatter.format(self.circuit.input_impedance, "Ω"))

    def _parse_output_nodes(self, output_str):
        """Parse LISO token as output node directive

        :param output_str: output node name, and (unused) plot scaling \
                           separated by colons
        :type output_str: str
        """

        # split options by colon
        options = output_str.split(":")

        # only use first option, which is the node name
        node = Node(options[0])

        LOGGER.info("adding output node %s", node)
        self.add_output_node(node)
        # FIXME: parse list of output nodes

    def _parse_noise_node(self, node_str):
        """Parse LISO token as noise node directive

        :param node_str: noise node name, and (unused) plot scaling \
                         separated by colons
        :type node_str: str
        """

        # split options by colon
        options = node_str.split(":")

        # only use first option, which is the node name
        node = Node(options[0])

        LOGGER.info("setting noise node %s", node)
        self.circuit.noise_node = node

        if len(options) > 1:
            LOGGER.warning("ignoring plot options in noise command")

    def _parse_frequencies(self, options):
        """Parse LISO input file frequency options

        :param options: frequency options
        :type options: Sequence[str]
        """

        if len(options) != 4:
            raise ValueError("syntax: freq lin|log start stop steps")

        start, _ = SIFormatter.parse(options[1])
        stop, _ = SIFormatter.parse(options[2])
        # steps + 1
        count = int(options[3]) + 1

        if options[0] == "lin":
            scaling_str = "linear"
            self.frequencies = np.linspace(start, stop, count)
        elif options[0] == "log":
            scaling_str = "logarithmic"
            self.frequencies = np.logspace(np.log10(start), np.log10(stop),
                                           count)
        else:
            raise ValueError("space function can be \"lin\" or \"log\"")

        LOGGER.info("simulating %i frequencies between %s and %s with %s "
                    "scaling", count, SIFormatter.format(start, "Hz"),
                    SIFormatter.format(stop, "Hz"), scaling_str)

    def solution(self, *args, **kwargs):
        """Get circuit solution

        Optional arguments are passed to :meth:`~Circuit.solve`.

        :return: solution
        :rtype: :class:`~Solution`
        """

        # solve
        return self.circuit.solve(frequencies=self.frequencies, *args, **kwargs)

class OutputParser(BaseParser):
    """LISO output parser"""

    # circuit definitions
    # match text after e.g. "#2 capacitors:" and before the first line with
    # a non-whitespace character after the "#"
    COMPONENT_REGEX = re.compile("^#(\d+) (op-amps?|capacitors?|resistors?|nodes?):([\s\S]+?)(?=\n#\S+)",
                                 re.MULTILINE)

    # data column definitions
    TF_VOLTAGE_OUTPUT_REGEX = re.compile("^\#OUTPUT (\d+) voltage outputs:$")
    TF_CURRENT_OUTPUT_REGEX = re.compile("^\#OUTPUT (\d+) current outputs:$")
    NOISE_OUTPUT_REGEX = re.compile("^\#Noise is computed at node ([\w\d]+) for .* :$")
    # "0 node: nin dB Degrees"
    TF_VOLTAGE_SINK_REGEX = re.compile("^\#\s*(\d+) node: ([\w\d]+) (\w+) (\w+)$")
    # "#  0 C:c2 dB Degrees"
    TF_CURRENT_SINK_REGEX = re.compile("^\#\s*(\d+) (\w+):([\w\d]+) (\w+) (\w+)$")
    # """#Noise is computed at node no for (nnoise=6, nnoisy=6) :
    #    #  r1 r3 r4 r6 op1(U) op1(I-) """
    NOISE_VOLTAGE_SOURCE_REGEX = re.compile("^\#Noise is computed at node [\w\d]+ for .* :\n\#\s*([\w\d\s\(\)\-\+]*)\s*$",
                                            re.MULTILINE)
    # "o1(I+)"
    NOISE_COMPONENT_REGEX = re.compile("^([\w\d]+)(\(([\w\d\-\+]*)\))?$")

    # input nodes
    INPUT_NODE_REGEX = re.compile("\#Voltage input at node ([\w\d]+), impedance (\d+) Ohm")

    def __init__(self, *args, **kwargs):
        # defaults
        self.data = None
        self.functions = []

        super(OutputParser, self).__init__(*args, **kwargs)

    def add_function(self, function):
        if function in self.functions:
            raise ValueError("duplicate function")

        self.functions.append(function)

    def solution(self):
        """Get circuit solution

        :return: solution
        :rtype: :class:`~Solution`
        """

        # create solution
        solution = Solution(self.circuit, self.frequencies)

        # add functions
        for function in self.functions:
            solution.add_function(function)

        return solution

    def parse_lines(self, lines):
        # parse data
        self.data = np.genfromtxt(self.filepath)

        # parse circuit and column definitions
        self._parse_circuit(lines)
        self._parse_columns(lines)

    def _parse_circuit(self, lines):
        text = "".join(lines)

        # find components
        for (count, description, content) in re.findall(self.COMPONENT_REGEX, text):
            if description.startswith(("resistor", "capacitor", "inductor")):
                self._parse_lcr(description, content)
            elif description.startswith("op-amp"):
                self._parse_opamp(description, content)
            elif description.startswith("node"):
                # nodes already defined by components
                continue

        # find input node(s)
        self._parse_input_nodes(lines)

    def _parse_lcr(self, description, content):
        # tokenise non-empty lines, stripping out comment hash
        for tokens in [self.tokenise(line.lstrip("#"))
                       for line in content.splitlines() if line]:
            name = tokens[1]

            # parse value
            value, _ = SIFormatter.parse(tokens[2] + tokens[3])

            # nodes
            node1_name = tokens[4]
            node2_name = tokens[5]

            # create component
            if description.startswith("resistor"):
                self._add_resistor(name, value, node1_name, node2_name)
            elif description.startswith("capacitor"):
                self._add_capacitor(name, value, node1_name, node2_name)
            elif description.startswith("inductor"):
                self._add_inductor(name, value, node1_name, node2_name)

    def _parse_opamp(self, description, content):
        # extract op-amp data
        pass

    def _parse_input_nodes(self, lines):
        for line in lines:
            match = re.match(self.INPUT_NODE_REGEX, line)

            if not match:
                continue

            # FIXME: support floating inputs
            self.circuit.input_node_p = Node(match.group(1))
            self.circuit.input_impedance = float(match.group(2))

            LOGGER.info("adding input node %s with impedance %s",
                        self.circuit.input_node_p,
                        SIFormatter.format(self.circuit.input_impedance, "Ω"))

    def _parse_columns(self, lines):
        for line in lines:
            if re.match(self.TF_VOLTAGE_OUTPUT_REGEX, line):
                self._parse_voltage_nodes(lines)
            elif re.match(self.TF_CURRENT_OUTPUT_REGEX, line):
                self._parse_current_components(lines)
            else:
                match = re.match(self.NOISE_OUTPUT_REGEX, line)

                if match:
                    self._parse_noise_components(match.group(1), lines)

    def _parse_voltage_nodes(self, lines):
        """Matches output file voltage transfer functions

        :param lines: output file lines
        :type lines: Sequence[str]
        """

        # transfer function source is the input
        source = self.circuit.input_node_p

        # find transfer functions
        for line in lines:
            match = re.match(self.TF_VOLTAGE_SINK_REGEX, line)

            if not match:
                continue

            # data column index
            column = int(match.group(1))

            # voltage sink node
            sink = Node(match.group(2))

            # data
            frequencies = self.data[:, 0] # frequency always first
            magnitude_data = self.data[:, column * 2 + 1]
            phase_data = self.data[:, column * 2 + 2]

            # scales
            magnitude_scale = match.group(3)
            phase_scale = match.group(4)

            # create data series
            series = ComplexSeries(x=frequencies, magnitude=magnitude_data,
                                   phase=phase_data,
                                   magnitude_scale=magnitude_scale,
                                   phase_scale=phase_scale)

            self.add_function(VoltageTransferFunction(series=series,
                                                      source=source,
                                                      sink=sink))

            # add output node
            self.add_output_node(sink)

    def _parse_current_components(self, lines):
        """Matches output file current transfer functions

        :param lines: output file lines
        :type lines: Sequence[str]
        """

        # transfer function source is the input
        source = self.circuit.input_node_p

        # find transfer functions
        for line in lines:
            match = re.match(self.TF_CURRENT_SINK_REGEX, line)

            if not match:
                continue

            # data column index
            column = int(match.group(1))

            # current sink component
            sink = self.circuit.get_component(match.group(3))

            # data
            frequencies = self.data[:, 0] # frequency always first
            magnitude_data = self.data[:, column * 2 + 1]
            phase_data = self.data[:, column * 2 + 2]

            # scales
            magnitude_scale = match.group(4)
            phase_scale = match.group(5)

            # create data series
            series = ComplexSeries(x=frequencies, magnitude=magnitude_data,
                                   phase=phase_data,
                                   magnitude_scale=magnitude_scale,
                                   phase_scale=phase_scale)

            self.add_function(CurrentTransferFunction(series=series,
                                                      source=source,
                                                      sink=sink))

            # add output node
            # FIXME: add output current components
            #self.add_output_node(sink)

    def _parse_noise_components(self, node_name, lines):
        """Matches output file noise spectra

        :param node_name: name of noise sink node
        :type node_name: str
        :param lines: output file lines
        :type lines: Sequence[str]
        """

        # find noise component information
        matches = re.search(self.NOISE_VOLTAGE_SOURCE_REGEX, "".join(lines))

        # noise sink is the noise node
        self.circuit.noise_node = Node(node_name)

        # split into list
        source_strs = matches.group(2).split()

        for index, source_str in enumerate(source_strs, start=1):
            # extract component and noise type
            source_name, noise_type = self._parse_noise_component(source_str)

            # noise source component
            source = self.circuit.get_component(source_name)

            frequencies = self.data[:, 0] # frequency always first
            spectrum = self.data[:, index]

            # create data series
            series = Series(x=frequencies, y=spectrum)

            self.add_function(NoiseSpectrum(series=series,
                                            source=source,
                                            sink=self.circuit.noise_node))#,
                                            #noise_type=noise_type)

    @classmethod
    def _parse_noise_component(cls, source_str):
        # get rid of whitespace around string
        source_str = source_str.strip()

        # look for component name and brackets
        match = re.match(cls.NOISE_COMPONENT_REGEX, source_str)

        component_name = match.group(1)

        # component noise type, e.g. I+ (or empty, for resistors)
        noise_str = match.group(3)

        # group 2 is not empty if noise type is specified
        if match.group(2):
            # op-amp noise; check first character
            if noise_str[0] == "U":
                noise_type = Circuit.NOISE_OPAMP_VOLTAGE
            elif noise_str[0] == "I":
                noise_type = Circuit.NOISE_OPAMP_CURRENT
            else:
                raise ValueError("unrecognised noise type")
        else:
            noise_type = Circuit.NOISE_JOHNSON

        return component_name, noise_type

class Runner(object):
    """LISO runner"""

    def __init__(self, script_path):
        self.script_path = script_path

    def run(self, plot=False, liso_path=None, output_path=None):
        self.liso_path = liso_path

        if not output_path:
            temp_file = NamedTemporaryFile()
            output_path = temp_file.name

        return self._liso_result(self.script_path, output_path, plot)

    def _liso_result(self, script_path, output_path, plot):
        """Get LISO results

        :param script_path: path to LISO ".fil" file
        :type script_path: str
        :param output_path: path to LISO ".out" file to be created
        :type output_path: str
        :param plot: whether to show result with gnuplot
        :type plot: bool
        :return: LISO output
        :rtype: :class:`~OutputParser`
        """

        result = self._run_liso_process(script_path, output_path, plot)

        if result.returncode != 0:
            raise Exception("error during LISO run")

        return OutputParser(output_path)

    def _run_liso_process(self, script_path, output_path, plot):
        input_path = os.path.abspath(script_path)

        if not os.path.exists(input_path):
            raise Exception("input file %s does not exist" % input_path)

        # LISO flags
        flags = [input_path, output_path]

        # plotting
        if not plot:
            flags.append("-n")

        liso_path = self.liso_path
        LOGGER.debug("using LISO binary at %s", liso_path)

        # run LISO
        return subprocess.run([liso_path, *flags])

    @property
    def liso_path(self):
        if self._liso_path is not None:
            return self._liso_path

        # use environment variable
        try:
            liso_dir = os.environ["LISO_DIR"]
        except KeyError:
            raise Exception("environment variable \"LISO_DIR\" must point to the "
                            "directory containing the LISO binary")

        return self.find_liso(liso_dir)

    @liso_path.setter
    def liso_path(self, path):
        LOGGER.debug("setting LISO binary path to %s", path)
        self._liso_path = path

    @staticmethod
    def find_liso(directory):
        if sys.platform.startswith("linux") or sys.platform.startswith("darwin"):
            # in order of preference
            filenames = ["fil_static", "fil"]
        elif sys.platform.startswith("win32"):
            filenames = ["fil.exe"]
        else:
            raise EnvironmentError("unrecognised operating system")

        for filename in filenames:
            path = os.path.join(directory, filename)

            if os.path.isfile(path):
                return path

        raise FileNotFoundError("no appropriate LISO binary found")