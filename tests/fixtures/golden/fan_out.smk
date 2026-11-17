rule all:
    input:
        "split-1-out",
        "piped-1-out",
        "fixed-1-out",
rule split_1:
    input:
        i_0="in-1-out",
    output:
        o_0="split-1-out",
    shell:
        "./t {input.i_0:q} > {output.o_0:q}"
rule piped_1:
    input:
        i_0="split-1-out",
    output:
        o_0="piped-1-out",
    shell:
        "./t < {input.i_0:q} > {output.o_0:q}"
rule fixed_1:
    input:
        i_0="split-1-out",
    output:
        o_0="fixed-1-out",
    shell:
        "./t -o {output.o_0:q} {input.i_0:q}"