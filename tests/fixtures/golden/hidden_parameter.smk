rule all:
    input:
        "sub-1-out",
rule sub_1:
    input:
        i_0="in-1-out",
    output:
        o_0="sub-1-out",
    shell:
        "./t run -l 3 {input.i_0:q} > {output.o_0:q}"