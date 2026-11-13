rule all:
    input:
        "fixed-1-out",
rule fixed_1:
    input:
        i_0="in-1-out",
    output:
        o_0="fixed-1-out",
    shell:
        "./t -o {output.o_0:q} {input.i_0:q}"